/** @odoo-module **/
/* global QUnit */

import {
    SharedReadPool,
    canonicalSharedReadJson,
    sharedReadsService,
} from "@contact_center_ui/js/contact_center_shared_reads.esm";
import {
    AutomaticReadOwner,
    AutomaticRefreshDeferred,
} from "@contact_center_ui/js/contact_center_refresh.esm";
import {ContactCenterStore} from "@contact_center_ui/js/contact_center_store.esm";
import {
    CONNECTION_HEALTH_EVENT_TYPE,
    CONTACT_CENTER_NOTIFICATION_TYPE,
} from "@contact_center_ui/js/contact_center_model.esm";

const MODEL = "contact.center.ui.api";
const METHOD = "get_connection_health";
const health = () => ({schema_version: 1, items: [], summary: {total: 0}});

async function settle() {
    for (let index = 0; index < 30; index++) {
        await Promise.resolve();
    }
}

function fixture() {
    const pending = [];
    const context = {
        userId: 7,
        companyId: 2,
        activeCompanyIds: [2, 3],
        lang: "pt_BR",
        tz: "America/Sao_Paulo",
        rpcContext: {allowed_company_ids: [2, 3], lang: "pt_BR"},
    };
    const orm = {
        user: {
            get userId() {
                return context.userId;
            },
            get context() {
                return context.rpcContext;
            },
        },
        call(model, method, args, kwargs) {
            let resolve = null,
                reject = null;
            const promise = new Promise((ok, fail) => {
                resolve = ok;
                reject = fail;
            });
            const request = {model, method, args, kwargs, resolve, reject, aborts: 0};
            promise.abort = (options = {}) => {
                request.aborts++;
                if (options.reject !== false) {
                    reject(new Error("native request aborted"));
                }
            };
            pending.push(request);
            return promise;
        },
    };
    const pool = new SharedReadPool({orm, getContext: () => context});
    return {pool, pending, context, orm};
}

function timer() {
    let next = 0;
    const tasks = new Map();
    return {
        tasks,
        setTimeout(callback, delay) {
            tasks.set(++next, {callback, delay});
            return next;
        },
        clearTimeout(id) {
            tasks.delete(id);
        },
        flush(delay) {
            for (const [id, task] of [...tasks]) {
                if (task.delay === delay) {
                    tasks.delete(id);
                    task.callback();
                }
            }
        },
    };
}

function storeFixture(orm, sharedReads, busService = new EventTarget()) {
    const visible = new EventTarget();
    visible.hidden = false;
    const clock = timer();
    const store = new ContactCenterStore({
        orm,
        sharedReads,
        busService,
        notification: false,
        realtimeTimer: clock,
        healthTimer: clock,
        refreshNow: () => 0,
        refreshRandom: () => 0,
        visibilityDocument: visible,
        focusTarget: new EventTarget(),
    });
    store.state.bootstrap = {user: {id: 7}};
    return store;
}

QUnit.module("contact_center_ui > shared health reads", () => {
    QUnit.test(
        "identical canonical inputs share only inflight and clone every result",
        async (assert) => {
            const {pool, pending} = fixture();
            const first = pool.call(MODEL, METHOD, [{b: 2, a: 1}], {
                context: {y: 2, x: 1},
            });
            const second = pool.call(MODEL, METHOD, [{a: 1, b: 2}], {
                context: {x: 1, y: 2},
            });
            assert.strictEqual(pending.length, 1);
            pending[0].resolve({items: [{value: 4}], nested: {ok: true}});
            const [one, two] = await Promise.all([first, second]);
            one.items[0].value = 99;
            one.nested.ok = false;
            assert.deepEqual(two, {items: [{value: 4}], nested: {ok: true}});
            assert.strictEqual(pool.entries.size, 0);
            assert.strictEqual(pool.running.size, 0);
            const third = pool.call(MODEL, METHOD, [{a: 1, b: 2}], {
                context: {x: 1, y: 2},
            });
            assert.strictEqual(
                pending.length,
                2,
                "settled results have no cache or TTL"
            );
            pending[1].resolve({items: []});
            await third;
            pool.destroy();
        }
    );

    QUnit.test(
        "args, provider context and ordered companies are separate authorities",
        async (assert) => {
            const {pool, pending, context} = fixture();
            const calls = [
                pool.call(MODEL, METHOD),
                pool.call(MODEL, METHOD, [1]),
                pool.call(MODEL, METHOD, [], {context: {provider: "one"}}),
                pool.call(MODEL, METHOD, [], {context: {provider: "two"}}),
            ];
            assert.strictEqual(pending.length, 4);
            calls.forEach((promise) => promise.catch(() => undefined));
            context.activeCompanyIds = [3, 2];
            context.companyId = 3;
            context.rpcContext.allowed_company_ids = [3, 2];
            const current = pool.call(MODEL, METHOD);
            assert.strictEqual(pending.length, 5, "company order remains meaningful");
            assert.ok(pending.slice(0, 4).every((request) => request.aborts === 1));
            pending[4].resolve({items: []});
            await current;
            const errors = await Promise.all(
                calls.map((promise) => promise.catch((error) => error))
            );
            assert.ok(
                errors.every((error) => error instanceof AutomaticRefreshDeferred)
            );
            pool.destroy();
        }
    );

    QUnit.test(
        "the exact allowlist denies other methods before ORM dispatch; non JSON health inputs do not share",
        async (assert) => {
            const {pool, pending} = fixture();
            const denied = [];
            for (const [model, method] of [
                [MODEL, "check_connection_health"],
                ["other.model", METHOD],
                [MODEL, "get_conversation"],
                [MODEL, "mark_seen"],
            ]) {
                denied.push(pool.call(model, method), pool.call(model, method));
            }
            const errors = await Promise.all(
                denied.map((promise) => promise.catch((error) => error))
            );
            assert.ok(errors.every((error) => error instanceof TypeError));
            assert.strictEqual(
                pending.length,
                0,
                "the pool never dispatches mutations or other APIs"
            );
            assert.strictEqual(pool.running.size, 0);
            const calls = [];
            for (const args of [[new Date()], [undefined]]) {
                calls.push(
                    pool.call(MODEL, METHOD, args),
                    pool.call(MODEL, METHOD, args)
                );
            }
            assert.strictEqual(
                pending.length,
                4,
                "allowed reads with unsupported input each have their own transport"
            );
            pending.forEach((request) => request.resolve({ok: true}));
            await Promise.all(calls);
            assert.throws(() => canonicalSharedReadJson([, 1]), TypeError);
            assert.throws(
                () =>
                    canonicalSharedReadJson({
                        get secret() {
                            return 1;
                        },
                    }),
                TypeError
            );
            assert.throws(() => canonicalSharedReadJson({value: Infinity}), TypeError);
            const cyclic = {};
            cyclic.self = cyclic;
            assert.throws(() => canonicalSharedReadJson(cyclic), TypeError);
            pool.destroy();
        }
    );

    QUnit.test(
        "abort one preserves the other and last abort settles even reject false",
        async (assert) => {
            const {pool, pending} = fixture();
            const one = pool.call(MODEL, METHOD);
            const two = pool.call(MODEL, METHOD);
            one.abort({reject: false});
            assert.ok(
                (await one.catch((error) => error)) instanceof AutomaticRefreshDeferred
            );
            assert.strictEqual(
                pending[0].aborts,
                0,
                "second consumer owns the transport"
            );
            pending[0].resolve({items: [1]});
            assert.deepEqual(await two, {items: [1]});
            const three = pool.call(MODEL, METHOD);
            const four = pool.call(MODEL, METHOD);
            three.abort();
            four.abort({reject: false});
            four.abort();
            assert.ok(
                (await four.catch((error) => error)) instanceof AutomaticRefreshDeferred
            );
            assert.strictEqual(pending[1].aborts, 1, "transport aborts exactly once");
            assert.strictEqual(pool.entries.size, 0);
            assert.strictEqual(
                pool.running.size,
                0,
                "an unsettled native abort is not retained"
            );
            pool.destroy();
        }
    );

    QUnit.test(
        "transport errors and non JSON results settle all subscribers and clean entries",
        async (assert) => {
            const {pool, pending} = fixture();
            for (const invalid of [false, true]) {
                const one = pool.call(MODEL, METHOD);
                const two = pool.call(MODEL, METHOD);
                const request = pending[pending.length - 1];
                if (invalid) {
                    request.resolve({value: new Date()});
                } else {
                    request.reject(new Error("network"));
                }
                const errors = await Promise.all(
                    [one, two].map((promise) => promise.catch((error) => error))
                );
                assert.ok(
                    errors.every(
                        (error) => error instanceof (invalid ? TypeError : Error)
                    )
                );
                assert.strictEqual(pool.entries.size, 0);
                assert.strictEqual(pool.running.size, 0);
            }
            pool.destroy();
        }
    );

    QUnit.test(
        "auth or RPC context change is checked again before delivery",
        async (assert) => {
            const {pool, pending, context} = fixture();
            const old = pool.call(MODEL, METHOD);
            context.userId = 8;
            context.rpcContext.lang = "en_US";
            pending[0].resolve({private: "old account"});
            assert.ok(
                (await old.catch((error) => error)) instanceof AutomaticRefreshDeferred
            );
            assert.strictEqual(pool.running.size, 0);
            assert.strictEqual(pending[0].aborts, 1);
            const fresh = pool.call(MODEL, METHOD);
            pending[1].resolve({private: "current account"});
            assert.deepEqual(await fresh, {private: "current account"});
            pool.destroy();
        }
    );

    QUnit.test(
        "deadline and owner destruction cancel only each owner's subscription",
        async (assert) => {
            const {pool, pending} = fixture();
            const firstTimer = timer();
            const owner = new AutomaticReadOwner({timer: firstTimer});
            const other = new AutomaticReadOwner({timer: timer()});
            const one = owner.read(pool.call(MODEL, METHOD));
            const two = other.read(pool.call(MODEL, METHOD));
            one.catch(() => undefined);
            two.catch(() => undefined);
            firstTimer.flush(30000);
            assert.ok(
                (await one.catch((error) => error)) instanceof AutomaticRefreshDeferred
            );
            assert.strictEqual(pending[0].aborts, 0);
            other.destroy();
            assert.ok(
                (await two.catch((error) => error)) instanceof AutomaticRefreshDeferred
            );
            assert.strictEqual(pending[0].aborts, 1);
            assert.strictEqual(pool.running.size, 0);
            owner.destroy();
            pool.destroy();
        }
    );

    QUnit.test(
        "central event and reconnect listener creates a causal barrier before store reads",
        async (assert) => {
            const {orm, pending} = fixture();
            const bus = new EventTarget();
            const shared = sharedReadsService.start(
                {services: {}},
                {
                    orm,
                    user: {userId: 7, context: {allowed_company_ids: [2, 3]}},
                    bus_service: bus,
                }
            );
            for (const event of [
                new CustomEvent("notification", {
                    detail: [
                        {
                            type: CONTACT_CENTER_NOTIFICATION_TYPE,
                            payload: {
                                schema_version: 1,
                                event_type: CONNECTION_HEALTH_EVENT_TYPE,
                            },
                        },
                    ],
                }),
                new Event("reconnect"),
            ]) {
                const old = shared.call(MODEL, METHOD);
                const oldRequest = pending[pending.length - 1];
                let fresh = null;
                const storeListener = () => {
                    fresh = shared.call(MODEL, METHOD);
                };
                bus.addEventListener(event.type, storeListener);
                const before = pending.length;
                bus.dispatchEvent(event);
                bus.removeEventListener(event.type, storeListener);
                assert.strictEqual(
                    pending.length,
                    before + 1,
                    "post-event consumer cannot join old GET"
                );
                oldRequest.resolve({generation: "old"});
                pending[pending.length - 1].resolve({generation: "fresh"});
                assert.deepEqual(await fresh, {generation: "fresh"});
                assert.deepEqual(
                    await old,
                    {generation: "old"},
                    "old consumers finish under their native guards"
                );
            }
            shared.destroy();
        }
    );

    QUnit.test(
        "main company and kwargs company overrides remain separate key authorities",
        async (assert) => {
            const {orm, pending} = fixture();
            const company = {currentCompany: {id: 2}, allowedCompanyIds: [2, 3]};
            const shared = sharedReadsService.start(
                {services: {company}},
                {
                    orm,
                    user: {userId: 7, context: {allowed_company_ids: [2, 3]}},
                    bus_service: new EventTarget(),
                }
            );
            const calls = [
                shared.call(MODEL, METHOD, [], {
                    context: {allowed_company_ids: [2, 3]},
                }),
                shared.call(MODEL, METHOD, [], {
                    context: {allowed_company_ids: [3, 2]},
                }),
                shared.call(MODEL, METHOD, [], {context: {company_id: 2}}),
                shared.call(MODEL, METHOD, [], {context: {company_id: 3}}),
            ];
            assert.strictEqual(
                pending.length,
                4,
                "effective RPC company overrides cannot join"
            );
            const first = shared.call(MODEL, METHOD);
            const second = shared.call(MODEL, METHOD);
            assert.strictEqual(
                pending.length,
                5,
                "same main company and active order can join"
            );
            company.currentCompany = {id: 3};
            const fresh = shared.call(MODEL, METHOD);
            assert.strictEqual(
                pending.length,
                6,
                "main company changed with unchanged active IDs"
            );
            assert.ok(pending.slice(0, 5).every((request) => request.aborts === 1));
            const errors = await Promise.all(
                [...calls, first, second].map((promise) =>
                    promise.catch((error) => error)
                )
            );
            assert.ok(
                errors.every((error) => error instanceof AutomaticRefreshDeferred)
            );
            pending[5].resolve(health());
            assert.deepEqual(await fresh, health());
            shared.destroy();
        }
    );

    QUnit.test(
        "real inbox and chat stores share health and destruction preserves the other",
        async (assert) => {
            const {pool, pending, orm} = fixture();
            const inbox = storeFixture(orm, pool);
            const chat = storeFixture(orm, pool);
            const first = inbox.fetchConnectionHealth();
            const second = chat.fetchConnectionHealth();
            assert.strictEqual(pending.length, 1);
            inbox.destroy();
            assert.strictEqual(
                pending[0].aborts,
                0,
                "chat's read survives inbox unmount"
            );
            pending[0].resolve(health());
            assert.notOk(await first);
            assert.ok(await second);
            chat.destroy();
            pool.destroy();
        }
    );

    QUnit.test(
        "real check mutation success and error retire old health generation before poll",
        async (assert) => {
            for (const failed of [false, true]) {
                const {pool, pending, orm} = fixture();
                const inbox = storeFixture(orm, pool);
                const chat = storeFixture(orm, pool);
                const old = inbox.fetchConnectionHealth();
                const check = chat.checkConnectionHealth();
                assert.strictEqual(pending[1].method, "check_connection_health");
                if (failed) {
                    pending[1].reject(new Error("failed check"));
                } else {
                    pending[1].resolve(health());
                }
                await check;
                const fresh = chat.fetchConnectionHealth();
                assert.strictEqual(
                    pending.length,
                    3,
                    "poll after either mutation outcome opens new GET"
                );
                pending[0].resolve(health());
                pending[2].resolve(health());
                await Promise.all([old, fresh]);
                inbox.destroy();
                chat.destroy();
                pool.destroy();
            }
            await settle();
        }
    );
});
