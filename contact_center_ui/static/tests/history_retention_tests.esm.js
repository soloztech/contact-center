/** @odoo-module **/
/* global QUnit */

import {
    ContactCenterStore,
    validateRetentionResponse,
} from "@contact_center_ui/js/contact_center_store.esm";
import {
    HistoryRetention,
    retentionDescription,
} from "@contact_center_ui/js/history_retention.esm";
import {
    click,
    getFixture,
    makeDeferred,
    mount,
    nextTick,
} from "@web/../tests/helpers/utils";
import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";

function policy(overrides = {}) {
    return {
        supported: true,
        enabled: true,
        preserve: false,
        effective: true,
        days: 7,
        can_manage: true,
        revision: 1,
        expired_before: false,
        ...overrides,
    };
}

function response(value = policy(), preview = false) {
    return {
        schema_version: 1,
        channel_id: 10,
        policy: value,
        ...(preview
            ? {
                  preview: {
                      message_count: 5,
                      media_count: 2,
                      cutoff: "2026-09-05 12:00:00",
                      confirmation_token: "proof-from-server",
                  },
              }
            : {}),
    };
}

async function panel(overrides = {}) {
    const target = getFixture();
    const env = await makeTestEnv();
    const store = {getRetentionPolicy: async () => response(), ...overrides};
    const component = await mount(HistoryRetention, target, {
        env,
        props: {store, channelId: 10, policy: false, visible: true, focusRequest: 0},
    });
    await nextTick();
    return {target, component, store};
}

QUnit.module("contact_center_ui > history retention", (hooks) => {
    hooks.beforeEach(() => {
        const banner = document.getElementById("oe_neutralize_banner");
        if (banner) {
            (banner.parentElement || banner).remove();
        }
    });

    QUnit.test("retention copy follows days and account/group exceptions", (assert) => {
        assert.ok(retentionDescription(policy({days: 12})).includes("mais de 12 dias"));
        assert.ok(retentionDescription(policy({days: 1})).includes("mais de 1 dia"));
        assert.ok(
            retentionDescription(policy({preserve: true})).includes(
                "Histórico preservado"
            )
        );
        assert.ok(
            retentionDescription(policy({enabled: false})).includes(
                "desativada nesta caixa"
            )
        );
        assert.ok(
            retentionDescription(policy({enabled: false, preserve: true})).includes(
                "continuará preservado"
            )
        );
    });

    QUnit.test(
        "response validation rejects cross-channel, inconsistent rules and invalid previews",
        (assert) => {
            assert.strictEqual(
                validateRetentionResponse(response(), 10).policy.days,
                7
            );
            assert.throws(() => validateRetentionResponse(response(), 20));
            for (const changes of [
                {days: 0},
                {days: "7"},
                {can_manage: "true"},
                {revision: -1},
                {preserve: true, effective: true},
            ]) {
                assert.throws(() =>
                    validateRetentionResponse(response(policy(changes)), 10)
                );
            }
            assert.ok(validateRetentionResponse(response(policy(), true), 10, true));
            for (const changes of [
                {message_count: -1},
                {media_count: "2"},
                {confirmation_token: ""},
                {cutoff: false},
            ]) {
                const value = response(policy(), true);
                Object.assign(value.preview, changes);
                assert.throws(() => validateRetentionResponse(value, 10, true));
            }
            assert.throws(() =>
                validateRetentionResponse(
                    response(policy({can_manage: false}), true),
                    10,
                    true
                )
            );
        }
    );

    QUnit.test(
        "agent sees the current warning but cannot preserve the group",
        async (assert) => {
            const {target, component} = await panel({
                getRetentionPolicy: async () =>
                    response(policy({days: 14, can_manage: false})),
            });
            assert.ok(target.textContent.includes("mais de 14 dias"));
            assert.containsNone(target, "[role='switch']");
            assert.containsNone(target, ".modal");
            assert.notOk(await component.togglePreserve());
        }
    );

    QUnit.test(
        "preserving waits for the server before changing the shared switch",
        async (assert) => {
            const pending = makeDeferred();
            const calls = [];
            const {target, component} = await panel({
                setRetentionPreserve: (channelId, preserve, token) => {
                    calls.push([channelId, preserve, token]);
                    return pending;
                },
            });
            await click(target, "[role='switch']");
            assert.strictEqual(
                target.querySelector("[role='switch']").getAttribute("aria-checked"),
                "false"
            );
            assert.ok(target.querySelector("[role='switch']").disabled);
            assert.containsNone(target, ".cc-history-retention__notice");
            pending.resolve(
                response(policy({preserve: true, effective: false, revision: 2}))
            );
            await nextTick();
            assert.deepEqual(calls, [[10, true, false]]);
            assert.ok(component.local.policy.preserve);
            assert.strictEqual(
                target.querySelector("[role='switch']").getAttribute("aria-checked"),
                "true"
            );
            assert.containsOnce(target, ".cc-history-retention__notice");
        }
    );

    QUnit.test(
        "resume requires visible impact and confirmation before changing preservation",
        async (assert) => {
            const preserved = policy({preserve: true, effective: false});
            const reads = [];
            const writes = [];
            const {target, component} = await panel({
                getRetentionPolicy: async (id, preview) => {
                    reads.push([id, preview]);
                    return response(preserved, preview);
                },
                setRetentionPreserve: async (...args) => {
                    writes.push(args);
                    return response(policy({revision: 2}));
                },
            });
            assert.notOk(
                await component.save(false),
                "active policy cannot resume without preview token"
            );
            await click(target, "[role='switch']");
            assert.containsOnce(target, ".cc-history-retention__preview");
            assert.ok(
                target.textContent
                    .replace(/\s+/g, " ")
                    .includes("5 mensagens e 2 mídias")
            );
            assert.deepEqual(writes, []);
            await click(target, ".cc-history-retention__actions .cc-secondary-button");
            assert.containsNone(target, ".cc-history-retention__preview");
            assert.ok(component.local.policy.preserve);
            await click(target, "[role='switch']");
            await click(target, ".cc-history-retention__actions .cc-primary-button");
            assert.deepEqual(writes, [[10, false, "proof-from-server"]]);
            assert.deepEqual(reads, [
                [10, false],
                [10, true],
                [10, true],
            ]);
            assert.notOk(component.local.policy.preserve);
        }
    );

    QUnit.test(
        "failure leaves preservation unchanged and disables retry until policy reload",
        async (assert) => {
            const {target, component} = await panel({
                setRetentionPreserve: async () => {
                    throw new Error("offline");
                },
            });
            await click(target, "[role='switch']");
            assert.notOk(component.local.policy.preserve);
            assert.containsOnce(target, "[role='alert']");
            assert.containsNone(target, ".cc-history-retention__notice");
            assert.ok(target.querySelector("[role='switch']").disabled);
            assert.notOk(await component.togglePreserve());
            await click(target, ".cc-history-retention__error button");
            assert.notOk(target.querySelector("[role='switch']").disabled);
        }
    );

    QUnit.test(
        "closing the contact panel ignores an in-flight preview response",
        async (assert) => {
            const pending = makeDeferred();
            const preserved = policy({preserve: true, effective: false});
            const {component} = await panel({
                getRetentionPolicy: async (_id, preview) =>
                    preview ? pending : response(preserved),
            });
            const loading = component.togglePreserve();
            component.__owl__.app.destroy();
            pending.resolve(response(preserved, true));
            assert.notOk(await loading);
            assert.notOk(component.local.preview);
        }
    );

    QUnit.test(
        "store validates confirmation and never updates another group's projection",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: new EventTarget(),
                notification: false,
            });
            store.state.conversations = [
                {channel_id: 10, state: "open", retention: policy()},
                {channel_id: 20, state: "open", retention: policy()},
            ];
            store.state.selectedChannelId = 20;
            store.scheduleSynchronization = () => undefined;
            const calls = [];
            store.call = async (...args) => {
                calls.push(args);
                return response(
                    policy({preserve: true, effective: false, revision: 2})
                );
            };
            assert.ok((await store.setRetentionPreserve(10, true)).policy.preserve);
            assert.ok(store.loadedConversation(10).retention.preserve);
            assert.notOk(store.loadedConversation(20).retention.preserve);
            assert.strictEqual(store.state.selectedChannelId, 20);
            assert.deepEqual(calls, [
                ["set_retention_preserve", [10, true], {confirmation_token: false}],
            ]);
            store.call = async () => ({...response(), channel_id: 20});
            await assert.rejects(store.setRetentionPreserve(10, false));
            assert.ok(store.loadedConversation(10).retention.preserve);
            store.destroy();
        }
    );

    QUnit.test(
        "purge invalidates late timeline pages and drops media/reply snapshots before reloading",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: new EventTarget(),
                notification: false,
            });
            store.state.conversations = [
                {
                    channel_id: 10,
                    state: "open",
                    unread_count: 2,
                    first_unread_message_id: 100,
                    last_message: {message_id: 101},
                },
            ];
            Object.assign(store.state, {
                selectedChannelId: 10,
                timelineChannelId: 10,
                messages: [
                    {message_id: 100},
                    {message_id: 101, reply_to: {message_id: 100, body: "expired"}},
                ],
                replyTo: {message_id: 100},
            });
            const pending = makeDeferred();
            store.call = async () => pending;
            const oldPage = store.loadTimeline({reset: true});
            const refresh = makeDeferred();
            store.refreshLoadedConversations = () => refresh;
            const loads = [];
            store.loadTimeline = async (options) => {
                loads.push(options);
                return true;
            };
            assert.ok(
                store.handleRetentionNotification({
                    channel_id: 10,
                    retention_purged: true,
                    removed_message_ids: [100],
                })
            );
            assert.deepEqual(
                store.state.messages,
                [],
                "surviving quoted copies are hidden too"
            );
            assert.notOk(store.state.replyTo);
            assert.notOk(store.selectedConversation.last_message);
            assert.strictEqual(store.state.selectedChannelId, 10);
            pending.resolve({
                schema_version: 1,
                channel_id: 10,
                items: [{message_id: 100}],
                has_more: false,
                next_before_message_id: false,
            });
            assert.notOk(
                await oldPage,
                "pre-purge page is stale and cannot resurrect content"
            );
            assert.deepEqual(store.state.messages, []);
            refresh.resolve(true);
            await nextTick();
            assert.deepEqual(
                loads,
                [{reset: true, anchorUnread: true}],
                "reload retains personal unread anchor instead of marking the tail"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "purge refresh failures and conversation switches never restore erased content",
        async (assert) => {
            for (const change of ["failure", "switch"]) {
                const store = new ContactCenterStore({
                    orm: {},
                    busService: new EventTarget(),
                    notification: false,
                });
                store.state.conversations = [
                    {channel_id: 10, state: "open"},
                    {channel_id: 20, state: "open"},
                ];
                Object.assign(store.state, {
                    selectedChannelId: 10,
                    timelineChannelId: 10,
                    messages: [{message_id: 100}],
                });
                const pending = makeDeferred();
                store.refreshLoadedConversations = () => pending;
                let loads = 0;
                store.loadTimeline = async () => {
                    loads++;
                    return true;
                };
                store.handleRetentionNotification({
                    channel_id: 10,
                    retention_purged: true,
                });
                if (change === "switch") {
                    store.state.selectedChannelId = 20;
                    store.state.timelineChannelId = 20;
                    store.state.messages = [{message_id: 200}];
                }
                pending.resolve(change === "switch");
                await nextTick();
                assert.strictEqual(loads, 0, change);
                assert.deepEqual(
                    store.state.messages,
                    change === "switch" ? [{message_id: 200}] : [],
                    change
                );
                if (change === "failure") {
                    assert.strictEqual(store.state.timelinePhase, "error");
                }
                store.destroy();
            }
        }
    );

    QUnit.test(
        "header indicator follows effective days and opens the retention section",
        (assert) => {
            const app = Object.create(ContactCenterApp.prototype);
            app.ui = {sidePanel: "crm"};
            app.store = {
                state: {detailsOpen: false, retentionFocusRequest: 0},
                selectedConversation: {channel_id: 10, retention: policy({days: 15})},
            };
            assert.strictEqual(app.retentionIndicator, "Histórico: 15 dias");
            app.openRetentionPanel();
            assert.strictEqual(app.ui.sidePanel, "contact");
            assert.ok(app.store.state.detailsOpen);
            assert.strictEqual(app.store.state.retentionFocusRequest, 1);
            app.store.selectedConversation.retention = policy({
                preserve: true,
                effective: false,
            });
            assert.strictEqual(app.retentionIndicator, "");
        }
    );
});
