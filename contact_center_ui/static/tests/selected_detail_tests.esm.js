/** @odoo-module **/
/* global QUnit */
import {AutomaticRefreshDeferred} from "@contact_center_ui/js/contact_center_refresh.esm";
import {conversationListRow} from "@contact_center_ui/js/contact_center_store.esm";
import {e2Deferred, e2Full, e2Store} from "./e2_test_helpers.esm";

QUnit.module("contact_center_ui > E2 selected authorized detail", () => {
    QUnit.test(
        "overlapping mutation answers converge urgently without overwriting a newer detail or waiting for bus",
        async (assert) => {
            for (const reverseAnswers of [false, true]) {
                const {store, server, calls, timer} = e2Store();
                await store.selectConversation(10);
                calls.length = 0;
                const first = e2Deferred(),
                    second = e2Deferred();
                let writes = 0;
                server.respond = (method) =>
                    method === "update_conversation"
                        ? ++writes === 1
                            ? first.promise
                            : second.promise
                        : undefined;
                const firstItem = e2Full(10, {
                        tags: [{id: 5, name: "First", color: 1}],
                    }),
                    latestItem = e2Full(10, {
                        tags: [{id: 6, name: "Latest", color: 2}],
                    });
                const firstWrite = store.updateConversation({tag_ids: [5]});
                const secondWrite = store.updateConversation({tag_ids: [6]});
                server.items = [latestItem];
                if (reverseAnswers) {
                    second.resolve({schema_version: 1, item: latestItem});
                    await secondWrite;
                    first.resolve({schema_version: 1, item: firstItem});
                    await firstWrite;
                } else {
                    first.resolve({schema_version: 1, item: firstItem});
                    await firstWrite;
                    second.resolve({schema_version: 1, item: latestItem});
                    await secondWrite;
                }
                assert.strictEqual(
                    store.selectedConversation.tags[0].id,
                    reverseAnswers ? 6 : 5,
                    "guarded answer does not replace already applied detail"
                );
                await timer.advance(119);
                assert.strictEqual(
                    calls.filter((call) => call.method === "list_conversations").length,
                    0
                );
                await timer.advance(1);
                assert.deepEqual(
                    calls.map((call) => call.method),
                    [
                        "update_conversation",
                        "update_conversation",
                        "list_conversations",
                        "get_conversation",
                    ]
                );
                assert.strictEqual(
                    store.selectedConversation.tags[0].id,
                    6,
                    "urgent read converges to latest server commit in both answer orders"
                );
                assert.ok(store.permissionReady);
                store.destroy();
            }
        }
    );
    QUnit.test(
        "detail refresh between mutation request and answer triggers urgent convergence within owner interval",
        async (assert) => {
            for (const retention of [false, true]) {
                const {store, server, calls, timer} = e2Store();
                await store.selectConversation(10);
                // A recent ordinary cycle would otherwise delay synchronization by 2s.
                store.scheduleSynchronization(false);
                await timer.advance(120);
                calls.length = 0;
                const pending = e2Deferred();
                const policy = {
                    supported: true,
                    enabled: true,
                    effective: true,
                    preserve: false,
                    can_manage: true,
                    days: 7,
                    revision: 2,
                };
                const latest = retention
                    ? e2Full(10, {retention: policy})
                    : e2Full(10, {responsible: {id: 7, name: "Agent"}});
                const method = retention
                    ? "set_retention_preserve"
                    : "update_conversation";
                server.respond = (name) =>
                    name === method ? pending.promise : undefined;
                const writing = retention
                    ? store.setRetentionPreserve(10, false)
                    : store.updateConversation({responsible_id: 7});
                await store.refreshSelectedConversation({silent: true});
                server.items = [latest];
                pending.resolve(
                    retention
                        ? {schema_version: 1, channel_id: 10, policy}
                        : {schema_version: 1, item: latest}
                );
                await writing;
                assert.ok(
                    retention
                        ? store.selectedConversation.retention.preserve
                        : !store.selectedConversation.responsible,
                    "answer cannot overwrite the intervening detail"
                );
                assert.ok(store.permissionReady);
                await timer.advance(120);
                assert.deepEqual(
                    calls.map((call) => call.method),
                    [
                        method,
                        "get_conversation",
                        "list_conversations",
                        "get_conversation",
                    ],
                    "urgent read bypasses the ordinary interval without bus/repair"
                );
                assert.deepEqual(
                    retention
                        ? store.selectedConversation.retention
                        : store.selectedConversation.responsible,
                    retention ? policy : latest.responsible
                );
                store.destroy();
            }
        }
    );
    QUnit.test(
        "mutation answer from an obsolete selection never refreshes another or reopened selection",
        async (assert) => {
            for (const [retention, reselect] of [
                [false, false],
                [true, false],
                [false, true],
                [true, true],
            ]) {
                const {store, server, calls, timer} = e2Store({
                    items: [e2Full(), e2Full(20)],
                });
                await store.selectConversation(10);
                const pending = e2Deferred();
                const method = retention
                    ? "set_retention_preserve"
                    : "update_conversation";
                server.respond = (name) =>
                    name === method ? pending.promise : undefined;
                const writing = retention
                    ? store.setRetentionPreserve(10, false)
                    : store.updateConversation({tag_ids: [5]});
                await store.selectConversation(20);
                if (reselect) {
                    await store.selectConversation(10);
                }
                calls.length = 0;
                pending.resolve(
                    retention
                        ? {
                              schema_version: 1,
                              channel_id: 10,
                              policy: {
                                  supported: true,
                                  enabled: true,
                                  effective: true,
                                  preserve: false,
                                  can_manage: true,
                                  days: 7,
                                  revision: 2,
                              },
                          }
                        : {
                              schema_version: 1,
                              item: e2Full(10, {name: "Old selection answer"}),
                          }
                );
                await writing;
                await timer.advance(2000);
                assert.deepEqual(
                    calls,
                    [],
                    "obsolete selection answer creates no synchronization"
                );
                assert.strictEqual(store.state.selectedChannelId, reselect ? 10 : 20);
                assert.ok(store.permissionReady);
                store.destroy();
            }
        }
    );
    QUnit.test(
        "mutation answers from an obsolete company epoch never schedule urgent synchronization",
        async (assert) => {
            for (const retention of [false, true]) {
                const {store, server, user, calls, timer} = e2Store();
                await store.selectConversation(10);
                const pending = e2Deferred();
                const method = retention
                    ? "set_retention_preserve"
                    : "update_conversation";
                server.respond = (name) =>
                    name === method ? pending.promise : undefined;
                const writing = (
                    retention
                        ? store.setRetentionPreserve(10, false)
                        : store.updateConversation({tag_ids: [5]})
                ).catch((error) => error);
                user.context = {...user.context, allowed_company_ids: [2]};
                calls.length = 0;
                pending.resolve(
                    retention
                        ? {
                              schema_version: 1,
                              channel_id: 10,
                              policy: {
                                  supported: true,
                                  enabled: true,
                                  effective: true,
                                  preserve: false,
                                  can_manage: true,
                                  days: 7,
                                  revision: 2,
                              },
                          }
                        : {schema_version: 1, item: e2Full(10)}
                );
                await writing;
                await timer.advance(2000);
                assert.deepEqual(
                    calls,
                    [],
                    "obsolete epoch cannot schedule reads for the new company"
                );
                assert.notOk(store.state.selectedChannelId);
                assert.notOk(store.permissionReady);
                store.destroy();
            }
        }
    );
    QUnit.test(
        "full action messages project only the native preview subset into rows",
        (assert) => {
            const full = e2Full();
            full.last_message.actions = {reply: true};
            full.last_message.reactions = [{emoji: "x", count: 1}];
            full.last_message.media = [
                {
                    kind: "image",
                    is_voice_note: false,
                    content_url: "/private-media",
                    id: 1,
                },
                {kind: "video", is_voice_note: false},
            ];
            full.last_message.author.email = "private@example.invalid";
            const row = conversationListRow(full);
            assert.strictEqual(row.last_message.body_text, "Hello");
            assert.strictEqual(row.last_message.actions, undefined);
            assert.strictEqual(row.last_message.reactions, undefined);
            assert.strictEqual(row.last_message.author.email, undefined);
            assert.deepEqual(row.last_message.media, [
                {kind: "image", is_voice_note: false},
            ]);
            assert.strictEqual(row.identity.partner, undefined);
        }
    );
    QUnit.test(
        "compact click starts one detail and timeline concurrently; reopen gets fresh authorization",
        async (assert) => {
            const pending = e2Deferred();
            const {store, calls, server} = e2Store({
                respond: (method) =>
                    method === "get_conversation" ? pending.promise : undefined,
            });
            const selecting = store.selectConversation(10);
            assert.deepEqual(
                calls.map((call) => call.method),
                ["get_conversation", "get_timeline"]
            );
            assert.notOk(store.permissionReady);
            assert.notOk(store.selectedConversation.can_send);
            pending.resolve({schema_version: 1, item: e2Full()});
            await selecting;
            assert.ok(store.permissionReady);
            assert.ok(store.selectedConversation.can_send);
            assert.strictEqual(
                store.state.conversations[0].identity.partner,
                undefined,
                "detail never enters row"
            );
            store.clearConversationSelection();
            server.respond = false;
            await store.selectConversation(10);
            assert.strictEqual(
                calls.filter((call) => call.method === "get_conversation").length,
                2
            );
            assert.strictEqual(
                calls.filter((call) => call.method === "get_timeline").length,
                2
            );
            store.destroy();
        }
    );
    QUnit.test(
        "directed, remembered, restored and start opening seed the same operation fullitem",
        async (assert) => {
            for (const operation of ["directed", "remembered", "restored", "start"]) {
                const {store, calls, server} = e2Store();
                if (operation === "directed") {
                    await store.openInitialConversation(10);
                }
                if (operation === "remembered") {
                    assert.strictEqual(
                        await store.restoreRememberedConversation(10),
                        "restored"
                    );
                }
                if (operation === "restored") {
                    await store.reopenRestoredConversation(10, e2Full());
                }
                if (operation === "start") {
                    server.respond = (method) =>
                        method === "start_conversation"
                            ? Promise.resolve({
                                  schema_version: 1,
                                  channel_id: 10,
                                  item: e2Full(),
                              })
                            : undefined;
                    await store.startConversation(1, "+551100000000");
                }
                assert.ok(store.selectedDetailReady, operation);
                assert.strictEqual(
                    calls.filter((call) => call.method === "get_conversation").length,
                    ["start", "restored"].includes(operation) ? 0 : 1,
                    "no duplicate fullget"
                );
                assert.strictEqual(
                    calls.filter((call) => call.method === "get_timeline").length,
                    1
                );
                store.destroy();
            }
        }
    );
    QUnit.test(
        "first error retains timeline and selection, automatic retry bounded and manual retry detail-only",
        async (assert) => {
            let failures = 2;
            const {store, calls, timer} = e2Store({
                respond: (method) =>
                    method === "get_conversation" && failures-- > 0
                        ? Promise.reject(new Error("temporary"))
                        : undefined,
            });
            await store.selectConversation(10);
            assert.strictEqual(store.state.selectedDetail.status, "error");
            assert.strictEqual(store.state.selectedChannelId, 10);
            await timer.advance(1000);
            assert.strictEqual(store.state.selectedDetail.status, "error");
            await timer.advance(5000);
            assert.strictEqual(
                calls.filter((call) => call.method === "get_conversation").length,
                2,
                "one bounded automatic retry"
            );
            await store.retrySelectedDetail();
            assert.ok(store.selectedDetailReady);
            assert.strictEqual(
                calls.filter((call) => call.method === "get_timeline").length,
                1
            );
            store.destroy();
        }
    );
    QUnit.test(
        "revalidation keeps authorized snapshot and linker/attribution through rows, failure and deferred",
        async (assert) => {
            const {store, server} = e2Store();
            await store.selectConversation(10);
            store.state.companyLinker = {
                ...store.state.companyLinker,
                open: true,
                channelId: 10,
                partnerId: 50,
            };
            store.state.attribution = {
                ...store.state.attribution,
                channelId: 10,
                phase: "ready",
                items: [{public_ref: "kept"}],
            };
            const previousPartner = store.selectedConversation.identity.partner;
            const row = conversationListRow(
                e2Full(10, {name: "New name", unread_count: 1})
            );
            store.replaceConversation(row);
            assert.strictEqual(
                store.selectedConversation.identity.partner,
                previousPartner
            );
            assert.strictEqual(store.selectedConversation.name, "New name");
            assert.ok(store.state.companyLinker.open);
            assert.strictEqual(store.state.attribution.items.length, 1);
            for (const error of [new Error("failed"), new AutomaticRefreshDeferred()]) {
                const request = e2Deferred();
                server.respond = (method) =>
                    method === "get_conversation" ? request.promise : undefined;
                const refreshing = store.refreshSelectedConversation({silent: true});
                assert.ok(
                    store.permissionReady,
                    "previous detail authorized while revalidating"
                );
                request.reject(error);
                await refreshing;
                assert.ok(store.permissionReady);
                assert.ok(store.selectedConversation.can_send);
            }
            server.respond = (method) =>
                method === "get_conversation"
                    ? Promise.reject({data: {name: "odoo.exceptions.AccessError"}})
                    : undefined;
            await store.refreshSelectedConversation({silent: true});
            assert.notOk(store.permissionReady);
            assert.notOk(store.selectedConversation);
            assert.notOk(store.state.companyLinker.open);
            store.destroy();
        }
    );
    QUnit.test(
        "nested identity/account change invalidates full detail; late selection or epoch denial never revokes newer selection",
        async (assert) => {
            const {store, server, user} = e2Store({items: [e2Full(), e2Full(20)]});
            await store.selectConversation(10);
            const pending = e2Deferred();
            server.respond = (method) =>
                method === "get_conversation" ? pending.promise : undefined;
            const refreshing = store.refreshSelectedConversation({silent: true});
            server.respond = false;
            await store.selectConversation(20);
            pending.reject({data: {name: "odoo.exceptions.AccessError"}});
            await refreshing;
            assert.strictEqual(store.state.selectedChannelId, 20);
            assert.ok(store.permissionReady);
            const changed = conversationListRow(
                e2Full(20, {
                    identity: {id: 999, name: "Merged", avatar_url: false},
                    account: {id: 2, name: "Other", platform: "whatsapp"},
                })
            );
            store.syncFullActive = true;
            store.replaceConversation(changed);
            store.syncFullActive = false;
            assert.notOk(store.permissionReady);
            assert.notOk(store.selectedConversation.can_send);
            user.context = {...user.context, allowed_company_ids: [2]};
            assert.notOk(store.permissionReady);
            assert.notOk(store.state.selectedChannelId);
            assert.strictEqual(store.state.conversations.length, 0);
            store.destroy();
        }
    );
    QUnit.test(
        "own update and link echo precede mutation answer without disabling or discarding current answer",
        async (assert) => {
            const {store, server} = e2Store({
                items: [
                    e2Full(10, {
                        identity: {
                            id: 110,
                            name: "Guest",
                            avatar_url: false,
                            partner: false,
                        },
                    }),
                ],
            });
            await store.selectConversation(10);
            const update = e2Deferred();
            server.respond = (method) =>
                method === "update_conversation" ? update.promise : undefined;
            const updating = store.updateConversation({tag_ids: [5]});
            store.synchronizeNotification({
                schema_version: 1,
                event_type: "conversation_updated",
                channel_id: 10,
            });
            assert.ok(store.selectedConversation.can_send);
            assert.ok(store.permissionReady);
            update.resolve({
                schema_version: 1,
                item: e2Full(10, {
                    identity: {
                        id: 110,
                        name: "Guest",
                        avatar_url: false,
                        partner: false,
                    },
                    tags: [{id: 5, name: "New", color: 2}],
                }),
            });
            await updating;
            assert.strictEqual(store.selectedConversation.tags[0].id, 5);
            assert.ok(store.openContactLinker());
            const link = e2Deferred();
            server.respond = (method) =>
                method === "link_partner" ? link.promise : undefined;
            const linking = store.linkPartner(50);
            store.synchronizeNotification({
                schema_version: 1,
                event_type: "identity_updated",
                channel_id: 10,
            });
            assert.ok(store.permissionReady);
            assert.ok(store.selectedConversation.can_send);
            link.resolve({
                schema_version: 1,
                identity: {...e2Full().identity, link_kind: "person"},
            });
            await linking;
            assert.strictEqual(store.selectedConversation.identity.partner.id, 50);
            assert.strictEqual(
                store.state.conversations[0].identity.partner,
                undefined
            );
            store.replaceConversation(conversationListRow(e2Full()));
            assert.strictEqual(
                store.selectedConversation.identity.partner.id,
                50,
                "row refresh retains mutation detail"
            );
            store.destroy();
        }
    );
    QUnit.test(
        "unlink, rename and retention answers update detail only and survive list refresh",
        async (assert) => {
            const {store, server} = e2Store();
            await store.selectConversation(10);
            store.applyIdentity(10, {
                ...e2Full().identity,
                partner: false,
                name: "Renamed",
            });
            assert.notOk(store.selectedConversation.identity.partner);
            assert.strictEqual(store.selectedConversation.identity.name, "Renamed");
            server.respond = (method) =>
                method === "set_retention_preserve"
                    ? Promise.resolve({
                          schema_version: 1,
                          channel_id: 10,
                          policy: {
                              supported: true,
                              enabled: true,
                              effective: true,
                              preserve: false,
                              can_manage: true,
                              days: 7,
                              revision: 2,
                          },
                      })
                    : undefined;
            await store.setRetentionPreserve(10, false);
            await store.refreshLoadedConversations({silent: true});
            assert.notOk(store.selectedConversation.identity.partner);
            assert.notOk(store.selectedConversation.retention.preserve);
            assert.strictEqual(store.state.conversations[0].retention, undefined);
            store.destroy();
        }
    );
});
