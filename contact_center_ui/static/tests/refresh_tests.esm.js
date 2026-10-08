/** @odoo-module **/
/* global QUnit */
import {
    AutomaticReadOwner,
    CoalescedRefresh,
} from "@contact_center_ui/js/contact_center_refresh.esm";
import {CONNECTION_HEALTH_EVENT_TYPE} from "@contact_center_ui/js/contact_center_model.esm";
import {ContactCenterStore} from "@contact_center_ui/js/contact_center_store.esm";

import {EventBus} from "@odoo/owl";
import {browser} from "@web/core/browser/browser";
import {jsonrpc} from "@web/core/network/rpc_service";
import {ormService} from "@web/core/orm_service";
import {LoadingIndicator} from "@web/webclient/loading_indicator/loading_indicator";
import {patchWithCleanup} from "@web/../tests/helpers/utils";

async function settle() {
    for (let i = 0; i < 30; i++) {
        await Promise.resolve();
    }
}
function clock() {
    let time = 0,
        next = 1;
    const tasks = new Map();
    return {
        tasks,
        now: () => time,
        setTimeout(callback, delay) {
            const id = next++;
            tasks.set(id, {callback, at: time + delay});
            return id;
        },
        clearTimeout(id) {
            tasks.delete(id);
        },
        async advance(ms) {
            const end = time + ms;
            for (;;) {
                const task = [...tasks]
                    .filter(([, v]) => v.at <= end)
                    .sort((a, b) => a[1].at - b[1].at)[0];
                if (!task) {
                    break;
                }
                tasks.delete(task[0]);
                time = task[1].at;
                task[1].callback();
                await settle();
            }
            time = end;
            await settle();
        },
    };
}
function deferred() {
    let resolve = null,
        reject = null;
    const promise = new Promise((ok, fail) => {
        resolve = ok;
        reject = fail;
    });
    let aborts = 0;
    promise.abort = () => {
        aborts++;
        reject(new Error("aborted"));
    };
    return {
        promise,
        resolve,
        reject,
        get aborts() {
            return aborts;
        },
    };
}
function storeFixture(timer = clock()) {
    const visible = new EventTarget();
    visible.hidden = false;
    const store = new ContactCenterStore({
        orm: {},
        busService: new EventTarget(),
        notification: false,
        realtimeTimer: timer,
        healthTimer: timer,
        refreshNow: timer.now,
        refreshRandom: () => 0,
        visibilityDocument: visible,
        focusTarget: new EventTarget(),
    });
    return {store, timer, visible};
}

QUnit.module("contact_center_ui > automatic refresh", () => {
    QUnit.test(
        "continuous events keep the leading deadline and space cycle starts",
        async (assert) => {
            const timer = clock(),
                starts = [],
                pending = [];
            const refresh = new CoalescedRefresh({
                timer,
                now: timer.now,
                run: () => {
                    starts.push(timer.now());
                    const request = deferred();
                    pending.push(request);
                    return request.promise;
                },
            });
            refresh.schedule();
            await timer.advance(100);
            refresh.schedule();
            await timer.advance(20);
            assert.deepEqual(starts, [120], "events cannot postpone the first cycle");
            for (let i = 0; i < 100; i++) {
                refresh.schedule();
            }
            await timer.advance(1000);
            assert.strictEqual(
                pending.length,
                1,
                "one active cycle despite sustained events"
            );
            pending[0].resolve(true);
            await settle();
            await timer.advance(999);
            assert.strictEqual(pending.length, 1);
            await timer.advance(1);
            assert.deepEqual(
                starts,
                [120, 2120],
                "one trailing cycle, two seconds between starts"
            );
            pending[1].resolve(true);
            await settle();
            refresh.destroy();
            assert.strictEqual(timer.tasks.size, 0);
        }
    );

    QUnit.test(
        "deadline aborts the actual request, backs off, and urgent events cannot bypass it",
        async (assert) => {
            const timer = clock(),
                starts = [],
                pending = [];
            let refresh = null;
            const reads = new AutomaticReadOwner({
                timer,
                onDeadline: () => refresh.noteDeadline(),
            });
            refresh = new CoalescedRefresh({
                timer,
                now: timer.now,
                random: () => 0,
                run: async () => {
                    starts.push(timer.now());
                    const request = deferred();
                    pending.push(request);
                    try {
                        await reads.read(request.promise);
                        return true;
                    } catch (_) {
                        return false;
                    }
                },
            });
            refresh.run();
            await timer.advance(30000);
            assert.strictEqual(pending[0].aborts, 1);
            refresh.schedule({urgent: true});
            await timer.advance(29999);
            assert.strictEqual(
                pending.length,
                1,
                "urgent and ordinary events respect deadline backoff"
            );
            await timer.advance(31);
            assert.deepEqual(starts, [0, 60030]);
            await timer.advance(30000);
            await timer.advance(60060);
            assert.deepEqual(
                starts,
                [0, 60030, 150090],
                "backoff increases after a second stalled read"
            );
            pending[2].resolve(true);
            await settle();
            assert.strictEqual(
                refresh.deadlines,
                0,
                "a complete successful cycle resets backoff"
            );
            reads.destroy();
            refresh.destroy();
            assert.strictEqual(timer.tasks.size, 0);
        }
    );

    QUnit.test(
        "destroy aborts owned reads and consumes late rejection without a retry",
        async (assert) => {
            const timer = clock(),
                request = deferred(),
                reads = new AutomaticReadOwner({timer});
            let failed = false;
            const result = reads.read(request.promise).catch(() => {
                failed = true;
            });
            reads.destroy();
            await result;
            assert.ok(failed);
            assert.strictEqual(request.aborts, 1);
            assert.strictEqual(timer.tasks.size, 0);
        }
    );

    QUnit.test(
        "hidden automatic work retains flags; direct actions remain independent",
        async (assert) => {
            const {store, timer, visible} = storeFixture();
            const origins = [];
            store.refreshLoadedConversations = async (options) => {
                origins.push(options);
                return true;
            };
            visible.hidden = true;
            store.scheduleSynchronization(true, true);
            await timer.advance(35000);
            assert.strictEqual(origins.length, 0);
            await store.refreshLoadedConversations({silent: true});
            assert.strictEqual(
                origins.length,
                1,
                "direct awaited refresh is not paused"
            );
            visible.hidden = false;
            store.onRefreshVisibility();
            await timer.advance(120);
            assert.deepEqual(origins[1], {silent: true, automatic: true});
            assert.notOk(store.syncTimeline);
            assert.notOk(store.syncReconnect);
            store.destroy();
            assert.strictEqual(timer.tasks.size, 0);
        }
    );

    QUnit.test(
        "waiters belong to the current list or the queued next list",
        async (assert) => {
            const {store, timer} = storeFixture();
            const requests = [];
            store.refreshLoadedConversations = () => {
                const request = deferred();
                requests.push(request);
                return request.promise;
            };
            store.scheduleSynchronization(false);
            let first = false,
                current = false,
                next = false;
            store.waitForScheduledSynchronization().then(() => {
                first = true;
            });
            await timer.advance(120);
            store.waitForScheduledSynchronization().then(() => {
                current = true;
            });
            store.scheduleSynchronization(false, true);
            store.waitForScheduledSynchronization().then(() => {
                next = true;
            });
            requests[0].resolve(true);
            await settle();
            assert.ok(first);
            assert.ok(current);
            assert.notOk(next, "old list cannot satisfy new invalidation");
            await timer.advance(2000);
            requests[1].resolve(true);
            await settle();
            assert.ok(next);
            store.scheduleSynchronization(false);
            let cancelled = false;
            store.waitForScheduledSynchronization().then(() => {
                cancelled = true;
            });
            store.destroy();
            await settle();
            assert.ok(cancelled);
            assert.strictEqual(store.syncTimer, null);
        }
    );

    QUnit.test(
        "automatic reads use silent ORM and deadline; direct silent reads have no added deadline",
        async (assert) => {
            const {store, timer} = storeFixture();
            const automatic = deferred(),
                direct = deferred();
            let normalCalls = 0,
                silentCalls = 0;
            store.orm = {
                call: () => {
                    normalCalls++;
                    return direct.promise;
                },
                silent: {
                    call: () => {
                        silentCalls++;
                        return silentCalls === 1 ? automatic.promise : direct.promise;
                    },
                },
            };
            const background = store
                .call("list_conversations", [], {}, {automatic: true})
                .catch(() => false);
            const foreground = store.call("get_conversation", [10], {}, {silent: true});
            await timer.advance(30000);
            assert.strictEqual(await background, false);
            assert.strictEqual(automatic.aborts, 1);
            assert.strictEqual(
                direct.aborts,
                0,
                "silent does not imply automatic origin"
            );
            assert.strictEqual(normalCalls, 0);
            assert.strictEqual(silentCalls, 2);
            direct.resolve({item: false});
            await foreground;
            store.destroy();
            assert.throws(
                () => store.call("send_text", [], {}, {automatic: true}),
                /restricted/
            );
        }
    );

    QUnit.test(
        "itemless health invalidation during RPC plus joined caller gets one fresh trailing read",
        async (assert) => {
            const {store, timer} = storeFixture();
            const requests = [],
                applied = [];
            store.call = (method) => {
                assert.strictEqual(method, "get_connection_health");
                const request = deferred();
                requests.push(request);
                return request.promise;
            };
            store.applyConnectionHealth = (payload) => {
                applied.push(payload);
                return true;
            };
            const first = store.refreshConnectionHealth();
            store.handleConnectionHealthNotification({
                event_type: CONNECTION_HEALTH_EVENT_TYPE,
            });
            store.refreshConnectionHealth();
            requests[0].resolve({schema_version: 1, items: [], checked_at: "old"});
            await first;
            assert.strictEqual(applied.length, 0);
            await timer.advance(160);
            assert.strictEqual(requests.length, 2, "exactly one trailing projection");
            requests[1].resolve({schema_version: 1, items: [], checked_at: "new"});
            await settle();
            assert.strictEqual(applied[0].checked_at, "new");
            store.destroy();
        }
    );

    QUnit.test(
        "the first automatic deadline ends the cycle and retains timeline/reconnect flags",
        async (assert) => {
            const {store, timer} = storeFixture();
            const request = deferred();
            let details = 0,
                timelines = 0,
                reads = 0;
            store.state.selectedChannelId = 10;
            store.orm = {
                silent: {
                    call: () => {
                        reads++;
                        return request.promise;
                    },
                },
            };
            store.refreshSelectedConversation = async () => {
                details++;
                return true;
            };
            store.refreshLatestTimeline = async () => {
                timelines++;
                return true;
            };
            store.scheduleSynchronization(true, true);
            await timer.advance(120);
            await timer.advance(30000);
            assert.strictEqual(request.aborts, 1);
            assert.strictEqual(details, 0);
            assert.strictEqual(timelines, 0);
            assert.ok(store.syncTimeline);
            assert.ok(store.syncReconnect);
            store.scheduleSynchronization(false, false, {urgent: true});
            await timer.advance(29999);
            assert.strictEqual(reads, 1);
            store.destroy();
            assert.strictEqual(timer.tasks.size, 0);
        }
    );

    QUnit.test(
        "hidden consistency timer still restarts an offline bus and repairs when visible",
        async (assert) => {
            const {store, timer, visible} = storeFixture();
            let busStarts = 0,
                lists = 0,
                health = 0;
            store.busService.start = () => {
                busStarts++;
            };
            store.call = async (method) => {
                assert.strictEqual(method, "get_connection_health");
                health++;
                return {schema_version: 1, items: []};
            };
            store.refreshLoadedConversations = async () => {
                lists++;
                return true;
            };
            store.started = true;
            store.state.realtime = "offline";
            visible.hidden = true;
            store.startConsistencySynchronization();
            await timer.advance(30000);
            assert.strictEqual(busStarts, 1);
            assert.strictEqual(health, 1);
            assert.strictEqual(lists, 0, "hidden tabs defer heavy reads");
            assert.notStrictEqual(
                store.consistencySyncTimer,
                null,
                "repair timer remains armed"
            );
            visible.hidden = false;
            store.onRefreshFocus();
            await timer.advance(120);
            assert.strictEqual(lists, 1);
            store.destroy();
            assert.strictEqual(timer.tasks.size, 0);
        }
    );

    QUnit.test(
        "hiding during a list RPC applies it and defers detail and timeline until visible",
        async (assert) => {
            const {store, timer, visible} = storeFixture();
            const request = deferred();
            const methods = [];
            const item = {channel_id: 10, name: "Comercial", state: "open"};
            store.state.selectedChannelId = 10;
            store.state.conversations = [item];
            const call = (_model, method) => {
                methods.push(method);
                return request.promise;
            };
            store.orm = {call, silent: {call}};
            store.scheduleSynchronization(true, true);
            await timer.advance(120);
            assert.deepEqual(methods, ["list_conversations"]);
            visible.hidden = true;
            store.onRefreshVisibility();
            request.resolve({
                schema_version: 1,
                items: [item],
                has_more: false,
                total: 1,
            });
            await settle();
            assert.strictEqual(store.state.listPhase, "ready");
            assert.strictEqual(store.state.conversations[0].channel_id, 10);
            assert.deepEqual(methods, ["list_conversations"]);
            assert.ok(store.syncReconnect);
            assert.ok(store.syncTimeline);
            assert.strictEqual(store.syncRefresh.deadlines, 0);
            await timer.advance(2000);
            assert.deepEqual(methods, ["list_conversations"]);
            visible.hidden = false;
            store.onRefreshVisibility();
            await timer.advance(119);
            assert.strictEqual(methods.length, 1);
            await timer.advance(1);
            assert.strictEqual(methods[1], "list_conversations");
            store.destroy();
            assert.strictEqual(timer.tasks.size, 0);
        }
    );

    QUnit.test(
        "urgent recovery keeps debounce and bypasses only the ordinary spacing",
        async (assert) => {
            const timer = clock(),
                starts = [];
            const refresh = new CoalescedRefresh({
                timer,
                now: timer.now,
                run: async () => {
                    starts.push(timer.now());
                    return true;
                },
            });
            await refresh.run();
            refresh.schedule();
            await timer.advance(100);
            refresh.schedule({urgent: true});
            await timer.advance(119);
            assert.deepEqual(starts, [0]);
            await timer.advance(1);
            assert.deepEqual(starts, [0, 220]);
            refresh.destroy();
        }
    );

    QUnit.test(
        "consecutive stalls bound abandoned server work for a 180 second server duration",
        async (assert) => {
            const timer = clock();
            let active = 0,
                peak = 0,
                calls = 0,
                refresh = null;
            const serverTimers = [];
            const reads = new AutomaticReadOwner({
                timer,
                onDeadline: () => refresh.noteDeadline(),
            });
            refresh = new CoalescedRefresh({
                timer,
                now: timer.now,
                random: () => 0,
                run: async () => {
                    calls++;
                    active++;
                    peak = Math.max(peak, active);
                    // A browser abort does not cancel the server's transaction.
                    serverTimers.push(
                        timer.setTimeout(() => {
                            active--;
                        }, 180000)
                    );
                    try {
                        await reads.read(deferred().promise);
                        return true;
                    } catch (_) {
                        return false;
                    }
                },
            });
            refresh.run();
            await timer.advance(900000);
            assert.ok(calls >= 6, "sustained stalls exercise the capped backoff");
            assert.strictEqual(
                peak,
                3,
                "per lane, under the stated server duration assumption"
            );
            reads.destroy();
            refresh.destroy();
            serverTimers.forEach((id) => timer.clearTimeout(id));
            assert.strictEqual(timer.tasks.size, 0);
        }
    );

    QUnit.test(
        "elapsed scheduling is independent of backwards wall-clock changes",
        async (assert) => {
            const timer = clock();
            let wallTime = 1000000;
            const starts = [];
            patchWithCleanup(performance, {now: timer.now}, {pure: true});
            patchWithCleanup(Date, {now: () => wallTime}, {pure: true});
            const refresh = new CoalescedRefresh({
                timer,
                run: async () => {
                    starts.push(timer.now());
                    return true;
                },
            });
            await refresh.run();
            wallTime -= 3600000;
            refresh.schedule();
            await timer.advance(1999);
            assert.deepEqual(starts, [0]);
            await timer.advance(1);
            assert.deepEqual(
                starts,
                [0, 2000],
                "monotonic spacing survives a clock correction"
            );
            refresh.destroy();
        }
    );

    QUnit.test(
        "fractional elapsed time rounds up to the first eligible timer",
        async (assert) => {
            const timer = clock(),
                starts = [];
            const integerTimers = {
                setTimeout: (callback, delay) =>
                    timer.setTimeout(callback, Math.trunc(delay)),
                clearTimeout: (id) => timer.clearTimeout(id),
            };
            const refresh = new CoalescedRefresh({
                timer: integerTimers,
                now: timer.now,
                run: async () => {
                    starts.push(timer.now());
                    return true;
                },
            });
            await timer.advance(1000.3);
            await refresh.run();
            await timer.advance(500.4);
            refresh.schedule();
            await timer.advance(1499);
            assert.strictEqual(starts.length, 1);
            await timer.advance(1);
            assert.strictEqual(
                starts.length,
                2,
                "first timer starts the cycle without another full debounce"
            );
            assert.ok(Math.abs(starts[1] - 3000.7) < 0.001);
            assert.strictEqual(timer.tasks.size, 0);
            refresh.destroy();
        }
    );

    QUnit.test(
        "applied health events fence stale RPCs; only a later caller needs a trailing read",
        async (assert) => {
            for (const [fullSnapshot, withJoiner] of [
                [false, false],
                [true, false],
                [false, true],
                [true, true],
            ]) {
                const {store, timer} = storeFixture();
                const request = deferred();
                let reads = 0;
                const item = {
                    id: 10,
                    account_name: "Comercial",
                    state: "connected",
                    checking: false,
                    last_check_at: "2026-10-08 15:00:00",
                };
                store.applyConnectionHealth({items: [{...item, last_check_at: "old"}]});
                store.call = () => {
                    reads++;
                    return reads === 1
                        ? request.promise
                        : Promise.resolve({schema_version: 1, items: [item]});
                };
                const first = store.refreshConnectionHealth();
                store.handleConnectionHealthNotification({
                    event_type: CONNECTION_HEALTH_EVENT_TYPE,
                    ...(fullSnapshot ? {connection_health: {items: [item]}} : {item}),
                });
                const joined = withJoiner ? store.refreshConnectionHealth() : false;
                request.resolve({
                    schema_version: 1,
                    items: [{...item, checking: true, last_check_at: "old"}],
                });
                assert.ok(await first);
                await joined;
                await timer.advance(1000);
                assert.strictEqual(
                    reads,
                    withJoiner ? 2 : 1,
                    "only a new caller after the applied event needs a trailing RPC"
                );
                assert.strictEqual(store.connectionHealth.summary.checking, 0);
                assert.strictEqual(
                    store.connectionHealth.items[0].last_check_at,
                    item.last_check_at,
                    "the late RPC cannot overwrite the bus result"
                );
                store.destroy();
            }
        }
    );

    QUnit.test(
        "healthy skipped history and superseded reads reset deadline backoff; failures do not",
        async (assert) => {
            const {store, timer} = storeFixture();
            store.state.selectedChannelId = 10;
            store.state.timelineHasMoreForward = true;
            store.refreshLoadedConversations = async () => true;
            store.refreshSelectedConversation = async () => true;
            store.syncTimeline = true;
            store.syncRefresh.noteDeadline();
            await timer.advance(30030);
            assert.strictEqual(
                store.syncRefresh.deadlines,
                0,
                "reading older history is a neutral successful cycle"
            );
            store.syncRefresh.noteDeadline();
            assert.strictEqual(
                store.syncRefresh.blockedUntil - timer.now(),
                30030,
                "an isolated later deadline starts at 30 seconds again"
            );
            store.destroy();

            for (const failed of [false, true]) {
                const fixture = storeFixture();
                const request = deferred();
                fixture.store.orm = {silent: {call: () => request.promise}};
                fixture.store.syncRefresh.deadlines = 1;
                const cycle = fixture.store.syncRefresh.run();
                fixture.store.listRequest++;
                if (failed) {
                    request.reject(new Error("transport failed"));
                } else {
                    request.resolve({schema_version: 1, items: [], has_more: false});
                }
                await cycle;
                assert.strictEqual(
                    fixture.store.syncRefresh.deadlines,
                    failed ? 1 : 0,
                    failed
                        ? "a rejected superseded read remains a failure"
                        : "a healthy superseded list is neutral"
                );
                fixture.store.destroy();
            }
        }
    );

    QUnit.test(
        "a detail clearing the selection preserves direct reload errors and automatic silence",
        async (assert) => {
            for (const automatic of [false, true]) {
                const {store} = storeFixture();
                const notices = [];
                store.notification = {add: (message, options) => notices.push(options)};
                store.state.selectedChannelId = 10;
                store.state.listPhase = "ready";
                store.replaceConversation = () => {
                    store.state.selectedChannelId = false;
                };
                const call = (_model, method) =>
                    method === "get_conversation"
                        ? Promise.resolve({schema_version: 1, item: false})
                        : Promise.reject(new Error("reload failed"));
                store.orm = {call, silent: {call}};
                await store.refreshSelectedConversation({silent: true, automatic});
                assert.strictEqual(
                    store.state.listPhase,
                    automatic ? "ready" : "error"
                );
                assert.strictEqual(notices.length, automatic ? 0 : 1);
                if (!automatic) {
                    assert.strictEqual(notices[0].title, "Conversas indisponíveis");
                }
                store.destroy();
            }
        }
    );

    QUnit.test(
        "focus skips a healthy recent visible tab and repairs pending offline or old state",
        async (assert) => {
            const {store, timer, visible} = storeFixture();
            let lists = 0,
                health = 0;
            store.state.realtime = "online";
            store.syncRefresh.lastStarted = 0;
            store.refreshLoadedConversations = async () => {
                lists++;
                return true;
            };
            store.scheduleConnectionHealthRefresh = () => {
                health++;
            };
            for (let i = 0; i < 20; i++) {
                store.onRefreshFocus();
            }
            await timer.advance(29999);
            assert.strictEqual(
                lists,
                0,
                "repeated focus adds no RPC to a fresh online tab"
            );
            assert.strictEqual(health, 0);
            await timer.advance(1);
            store.onRefreshFocus();
            await timer.advance(120);
            assert.strictEqual(lists, 1, "old snapshots repair on focus");
            store.scheduleSynchronization(false);
            store.onRefreshFocus();
            await timer.advance(2000);
            assert.strictEqual(
                lists,
                2,
                "focus flushes a pending cycle without overlap"
            );
            store.state.realtime = "offline";
            store.onRefreshFocus();
            await timer.advance(2000);
            assert.strictEqual(lists, 3, "offline state repairs on focus");
            visible.hidden = true;
            store.onRefreshFocus();
            await timer.advance(2000);
            assert.strictEqual(lists, 3);
            assert.strictEqual(health, 3);
            store.destroy();
        }
    );

    QUnit.test(
        "real silent ORM transport neither emits RPC:REQUEST nor blocks the Odoo UI",
        async (assert) => {
            const {store, timer} = storeFixture();
            const bus = new EventBus();
            let requests = 0,
                blocks = 0,
                aborts = 0,
                rpcId = 0;
            const indicator = {
                state: {count: 0, show: false},
                rpcIds: new Set(),
                uiService: {
                    block: () => {
                        blocks++;
                    },
                    unblock: () => undefined,
                },
            };
            bus.addEventListener("RPC:REQUEST", (event) => {
                requests++;
                LoadingIndicator.prototype.requestCall.call(indicator, event);
            });
            bus.addEventListener("RPC:RESPONSE", (event) =>
                LoadingIndicator.prototype.responseCall.call(indicator, event)
            );
            class StalledXHR extends EventTarget {
                open() {
                    /* Transport configuration is intentionally simulated. */
                }
                setRequestHeader() {
                    /* Headers are irrelevant to this stalled XHR. */
                }
                send() {
                    /* The request intentionally stays pending. */
                }
                abort() {
                    aborts++;
                }
            }
            patchWithCleanup(
                browser,
                {
                    XMLHttpRequest: StalledXHR,
                    setTimeout: timer.setTimeout.bind(timer),
                    clearTimeout: timer.clearTimeout.bind(timer),
                },
                {pure: true}
            );
            store.orm = ormService.start(
                {bus},
                {
                    user: {context: {}},
                    rpc: (url, params, options) =>
                        jsonrpc({bus}, ++rpcId, url, params, options),
                }
            );
            const automatic = store
                .call("list_conversations", [], {}, {automatic: true})
                .catch(() => false);
            await timer.advance(30000);
            assert.strictEqual(await automatic, false);
            assert.strictEqual(requests, 0);
            assert.strictEqual(blocks, 0);
            assert.strictEqual(
                aborts,
                1,
                "native abortable ORM promise reached the actual XHR"
            );
            const direct = store.call("get_conversation", [10]);
            direct.catch(() => undefined);
            await timer.advance(3250);
            assert.strictEqual(
                requests,
                1,
                "explicit reads retain ordinary Odoo loading feedback"
            );
            assert.strictEqual(
                blocks,
                1,
                "the real loading indicator control would block a stalled ordinary read"
            );
            direct.abort();
            await settle();
            store.destroy();
            assert.strictEqual(timer.tasks.size, 0);
        }
    );
});
