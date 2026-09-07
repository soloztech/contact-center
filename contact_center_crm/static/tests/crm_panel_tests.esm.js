/** @odoo-module **/
/* global QUnit */

import "@contact_center_crm/js/contact_center_app_crm.esm";
import {
    CUSTOMER_TABS,
    CrmPanel,
    CrmPanelModel,
    normalizeCustomerPage,
} from "@contact_center_crm/js/crm_panel.esm";
import {
    click,
    getFixture,
    makeDeferred,
    mount,
    nextTick,
} from "@web/../tests/helpers/utils";
import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";
import {makeFakeLocalizationService} from "@web/../tests/helpers/mock_services";

function customerRecord(id, tab = "opportunities", overrides = {}) {
    return {
        id,
        name: `Registro ${id}`,
        model: CUSTOMER_TABS.find((entry) => entry.id === tab).model,
        state: {key: "open", label: "Em aberto"},
        amount: 1000,
        currency: {id: 1, symbol: "R$", position: "before"},
        date: "2026-09-07",
        ...(tab === "opportunities"
            ? {
                  type: "opportunity",
                  active: true,
                  stage: {id: 1, name: "Qualificação", is_won: false},
                  team: {id: 1, name: "Comercial"},
                  user: false,
              }
            : {}),
        ...overrides,
    };
}

function page(items = [customerRecord(11), customerRecord(12)], overrides = {}) {
    return {
        schema_version: 1,
        channel_id: 404,
        tab: "opportunities",
        status: "ready",
        partner: {id: 21, name: "Pessoa de teste"},
        commercial_partner: {id: 22, name: "Empresa de teste"},
        tabs: CUSTOMER_TABS.map(({id}) => ({id, available: true})),
        items,
        has_more: false,
        ...overrides,
    };
}

function modelFor(call, action = {doAction: () => Promise.resolve()}) {
    const store = {
        state: {selectedChannelId: 404},
        capabilities: {view_crm: true},
        call,
        selectedConversation: {
            channel_id: 404,
            identity: {partner: {id: 21, company: {id: 22}}},
        },
    };
    return new CrmPanelModel({store, channelId: 404, action});
}

function responseFor(tab, ids = [11], overrides = {}) {
    return page(
        ids.map((id) => customerRecord(id, tab)),
        {tab, ...overrides}
    );
}

QUnit.module("contact_center_crm > customer records", (hooks) => {
    hooks.beforeEach(() => {
        makeFakeLocalizationService();
        const banner = document.getElementById("oe_neutralize_banner");
        if (banner) {
            (banner.parentElement || banner).remove();
        }
    });

    QUnit.test(
        "normalizes customer records without conversation link state",
        (assert) => {
            const result = normalizeCustomerPage(page(), 404, "opportunities");
            assert.strictEqual(result.partner.id, 21);
            assert.strictEqual(result.company.id, 22);
            assert.strictEqual(result.items[0].amount, 1000);
            assert.strictEqual(result.items[0].user, false);
            assert.strictEqual(result.items[0].date, "2026-09-07");
            assert.notOk("linked" in result.items[0]);
            assert.deepEqual(
                result.tabs.map(({id}) => id),
                ["opportunities", "quotations", "orders", "invoices"]
            );
            const credit = normalizeCustomerPage(
                page(
                    [
                        customerRecord(13, "invoices", {
                            type: "out_refund",
                            type_label: "Nota de crédito",
                            amount: -100,
                            payment_state: {key: "paid", label: "Pago"},
                        }),
                    ],
                    {tab: "invoices"}
                ),
                404,
                "invoices"
            ).items[0];
            assert.strictEqual(credit.amount, -100);
            assert.strictEqual(credit.paymentState, "Pago");
            assert.strictEqual(credit.typeLabel, "Nota de crédito");
        }
    );

    QUnit.test(
        "currency precision follows currency metadata with safe fallback",
        (assert) => {
            for (const [precision, expected] of [
                [3, 3],
                [0, 0],
                [-1, 2],
                [100, 2],
                ["3", 2],
            ]) {
                const item = normalizeCustomerPage(
                    page([
                        customerRecord(11, "opportunities", {
                            amount: 1.234,
                            currency: {
                                id: 1,
                                symbol: "USD",
                                position: "after",
                                decimal_places: precision,
                            },
                        }),
                    ]),
                    404,
                    "opportunities"
                ).items[0];
                assert.strictEqual(item.currency.decimalPlaces, expected);
                if (precision === 3) {
                    assert.ok(
                        /[.,]234 USD$/.test(CrmPanel.prototype.amountLabel(item))
                    );
                }
            }
        }
    );

    QUnit.test(
        "rejects mismatched conversations, tabs, models and pagination",
        (assert) => {
            assert.throws(() => normalizeCustomerPage(page(), 405, "opportunities"));
            assert.throws(() => normalizeCustomerPage(page(), 404, "orders"));
            assert.throws(() =>
                normalizeCustomerPage(
                    page([], {schema_version: 2}),
                    404,
                    "opportunities"
                )
            );
            assert.throws(() =>
                normalizeCustomerPage(
                    page([customerRecord(11, "orders")]),
                    404,
                    "opportunities"
                )
            );
            assert.throws(() =>
                normalizeCustomerPage(
                    page([customerRecord(11, "opportunities", {model: "res.users"})]),
                    404,
                    "opportunities"
                )
            );
            assert.throws(() =>
                normalizeCustomerPage(page([], {has_more: true}), 404, "opportunities")
            );
            assert.throws(() =>
                normalizeCustomerPage(
                    page([], {tabs: [{id: "opportunities", available: true}]}),
                    404,
                    "opportunities"
                )
            );
            const duplicate = CUSTOMER_TABS.map(() => ({
                id: "opportunities",
                available: true,
            }));
            assert.throws(() =>
                normalizeCustomerPage(page([], {tabs: duplicate}), 404, "opportunities")
            );
        }
    );

    QUnit.test(
        "unavailable tabs cannot expose records or accept truthy permissions",
        (assert) => {
            const tabs = CUSTOMER_TABS.map(({id}) => ({id, available: false}));
            const unavailable = page(undefined, {tabs, status: "unavailable"});
            const result = normalizeCustomerPage(unavailable, 404, "opportunities");
            assert.deepEqual(result.items, []);
            assert.notOk(result.hasMore);
            assert.strictEqual(result.phase, "unavailable");
            assert.throws(() =>
                normalizeCustomerPage(
                    {...unavailable, status: "ready"},
                    404,
                    "opportunities"
                )
            );
            assert.throws(() =>
                normalizeCustomerPage(
                    {
                        ...unavailable,
                        tabs: tabs.map((tab) => ({...tab, available: "false"})),
                    },
                    404,
                    "opportunities"
                )
            );
        }
    );

    QUnit.test(
        "capability controls all commercial tabs without Kanban",
        async (assert) => {
            const calls = [];
            const model = modelFor(async (...args) => {
                calls.push(args);
                return page();
            });
            model.store.capabilities.view_crm = false;
            assert.notOk(await model.load());
            assert.deepEqual(calls, []);
            model.store.capabilities.view_crm = true;
            assert.ok(await model.load());
            assert.deepEqual(calls, [
                ["get_customer_records", [404, "opportunities", "", 0, 20]],
            ]);
            assert.strictEqual(model.page.phase, "ready");
        }
    );

    QUnit.test(
        "commercial and contact controls select one panel keyed by customer",
        (assert) => {
            const app = Object.create(ContactCenterApp.prototype);
            app.crmUi = {panelMode: "contact"};
            app.store = {
                capabilities: {view_crm: true},
                state: {detailsOpen: true},
                selectedConversation: {
                    channel_id: 404,
                    identity: {partner: {id: 21, company: {id: 22}}},
                },
                toggleDetails() {
                    this.state.detailsOpen = !this.state.detailsOpen;
                },
            };
            app.toggleCrmPanel();
            assert.ok(app.crmPanelSelected);
            assert.ok(app.store.state.detailsOpen);
            app.toggleCrmPanel();
            assert.notOk(app.store.state.detailsOpen);
            app.toggleCrmPanel();
            app.toggleContactPanel();
            assert.notOk(app.crmPanelSelected);
            assert.ok(app.store.state.detailsOpen);
            app.toggleContactPanel();
            assert.notOk(app.store.state.detailsOpen);
            const before = app.crmPanelKey;
            app.store.selectedConversation.identity.partner.id = 24;
            assert.notEqual(app.crmPanelKey, before);
            app.store.capabilities.view_crm = false;
            app.toggleCrmPanel();
            assert.notOk(app.store.state.detailsOpen);
        }
    );

    QUnit.test(
        "changed conversation or customer discards pending response",
        async (assert) => {
            for (const change of [
                (store) => {
                    store.state.selectedChannelId = 405;
                },
                (store) => {
                    store.selectedConversation.identity.partner.id = 24;
                },
                (store) => {
                    store.selectedConversation.identity.partner.company.id = 25;
                },
            ]) {
                const pending = makeDeferred();
                const model = modelFor(() => pending);
                const load = model.load();
                change(model.store);
                pending.resolve(page());
                assert.notOk(await load);
                assert.deepEqual(model.page.items, []);
            }
        }
    );

    QUnit.test(
        "closing and reopening cannot reuse a previous response",
        async (assert) => {
            const pending = makeDeferred();
            const previous = modelFor(() => pending);
            const load = previous.load();
            previous.destroy();
            const current = modelFor(async () => page([customerRecord(13)]));
            await current.load();
            pending.resolve(page());
            assert.notOk(await load);
            assert.deepEqual(
                current.page.items.map((item) => item.id),
                [13]
            );
        }
    );

    QUnit.test("new searches win over slow previous searches", async (assert) => {
        const pending = makeDeferred();
        let count = 0;
        const model = modelFor(() => {
            count += 1;
            return count === 1 ? pending : Promise.resolve(page([customerRecord(13)]));
        });
        const oldLoad = model.load();
        model.page.query = "Novo";
        await model.load();
        pending.resolve(page());
        assert.notOk(await oldLoad);
        assert.deepEqual(
            model.page.items.map((item) => item.id),
            [13]
        );
        assert.strictEqual(model.page.appliedQuery, "Novo");
    });

    QUnit.test(
        "switching tabs rejects slow data and slow access errors from old tab",
        async (assert) => {
            for (const denied of [false, true]) {
                const pending = makeDeferred();
                const model = modelFor((_method, args) =>
                    args[1] === "opportunities"
                        ? pending
                        : Promise.resolve(responseFor("orders", [14]))
                );
                const oldLoad = model.load();
                await model.selectTab("orders");
                if (denied) {
                    pending.reject(
                        Object.assign(new Error("Denied"), {
                            data: {name: "odoo.exceptions.AccessError"},
                        })
                    );
                } else {
                    pending.resolve(page());
                }
                assert.notOk(await oldLoad);
                assert.strictEqual(model.page.phase, "ready");
                assert.strictEqual(model.state.activeTab, "orders");
                assert.deepEqual(
                    model.page.items.map((item) => item.id),
                    [14]
                );
                assert.strictEqual(model.state.partner.id, 21);
            }
        }
    );

    QUnit.test(
        "tabs retain independent searches and reload only their own records",
        async (assert) => {
            const calls = [];
            const model = modelFor(async (_method, args) => {
                calls.push(args);
                return responseFor(args[1]);
            });
            model.page.query = "Solar";
            await model.load();
            await model.selectTab("quotations");
            assert.strictEqual(model.page.query, "");
            model.page.query = "S0001";
            await model.load();
            await model.selectTab("opportunities");
            assert.strictEqual(model.page.query, "Solar");
            await model.selectTab("quotations");
            assert.strictEqual(model.page.query, "S0001");
            assert.deepEqual(calls[calls.length - 1], [
                404,
                "quotations",
                "S0001",
                0,
                20,
            ]);
            assert.notOk(model.selectTab("unknown"));
        }
    );

    QUnit.test("known unavailable tab makes no data request", async (assert) => {
        const calls = [];
        const tabs = CUSTOMER_TABS.map(({id}) => ({
            id,
            available: id === "opportunities",
        }));
        const model = modelFor(async (...args) => {
            calls.push(args);
            return page(undefined, {tabs});
        });
        await model.load();
        assert.ok(await model.selectTab("invoices"));
        assert.strictEqual(model.page.phase, "unavailable");
        assert.deepEqual(model.page.items, []);
        assert.strictEqual(calls.length, 1);
        await model.selectTab("opportunities");
        assert.strictEqual(calls.length, 2);
    });

    QUnit.test(
        "pagination keeps submitted tab search and deduplicates records",
        async (assert) => {
            const calls = [];
            const model = modelFor(async (_method, args) => {
                calls.push(args);
                return args[3]
                    ? page([customerRecord(11), customerRecord(12)])
                    : page([customerRecord(11)], {has_more: true});
            });
            model.page.query = "  Solar  ";
            await model.load();
            model.page.query = "Busca ainda não enviada";
            await model.load({append: true});
            assert.deepEqual(calls, [
                [404, "opportunities", "Solar", 0, 20],
                [404, "opportunities", "Solar", 1, 20],
            ]);
            assert.deepEqual(
                model.page.items.map((item) => item.id),
                [11, 12]
            );
            assert.notOk(model.page.hasMore);
        }
    );

    QUnit.test(
        "a server-side customer change restarts pagination without mixing customers",
        async (assert) => {
            const pending = makeDeferred();
            const calls = [];
            const newCustomer = {
                partner: {id: 31, name: "Novo cliente"},
                commercial_partner: {id: 32, name: "Nova empresa"},
            };
            const model = modelFor(async (_method, args) => {
                calls.push(args);
                if (calls.length === 1) {
                    return page([customerRecord(11)], {has_more: true});
                }
                return calls.length === 2
                    ? page([customerRecord(13)], newCustomer)
                    : pending;
            });
            model.page.query = "Solar";
            await model.load();
            model.page.query = "Busca ainda não enviada";
            model.state.pages.orders.items = [customerRecord(50, "orders")];
            const append = model.load({append: true});
            await nextTick();
            assert.notOk(
                model.state.partner,
                "old header is cleared while the new first page loads"
            );
            assert.ok(
                Object.values(model.state.pages).every(
                    (entry) => entry.items.length === 0
                )
            );
            assert.deepEqual(calls[2], [404, "opportunities", "Solar", 0, 20]);
            pending.resolve(page([customerRecord(12)], newCustomer));
            assert.ok(await append);
            assert.deepEqual(
                model.page.items.map((item) => item.id),
                [12]
            );
            assert.strictEqual(model.state.partner.id, 31);
            assert.strictEqual(model.page.query, "Busca ainda não enviada");
            assert.strictEqual(model.page.appliedQuery, "Solar");
            assert.strictEqual(
                model.customerKey,
                "21:22",
                "server response does not replace the store identity guard"
            );
        }
    );

    QUnit.test("slow pagination cannot enter another tab", async (assert) => {
        const pending = makeDeferred();
        const model = modelFor((_method, args) => {
            if (args[1] === "orders") {
                return Promise.resolve(responseFor("orders", [20]));
            }
            return args[3]
                ? pending
                : Promise.resolve(page([customerRecord(11)], {has_more: true}));
        });
        await model.load();
        const append = model.load({append: true});
        assert.notOk(await model.load({append: true}), "duplicate pagination ignored");
        await model.selectTab("orders");
        pending.resolve(page([customerRecord(12)]));
        assert.notOk(await append);
        assert.deepEqual(
            model.page.items.map((item) => item.id),
            [20]
        );
    });

    QUnit.test(
        "RPC errors retry and denied access clears all customer projections",
        async (assert) => {
            let mode = "error";
            const model = modelFor(async (_method, args) => {
                if (mode === "error") {
                    throw new Error("Network");
                }
                if (mode === "denied") {
                    throw Object.assign(new Error("Denied"), {
                        data: {name: "odoo.exceptions.AccessError"},
                    });
                }
                return responseFor(args[1]);
            });
            assert.notOk(await model.load());
            assert.strictEqual(model.page.phase, "error");
            mode = "ready";
            assert.ok(await model.load());
            await model.selectTab("invoices");
            mode = "denied";
            assert.notOk(await model.load());
            assert.strictEqual(model.page.phase, "denied");
            assert.notOk(model.state.partner);
            assert.notOk(model.state.company);
            assert.ok(
                Object.values(model.state.pages).every(
                    (entry) => entry.items.length === 0
                )
            );
        }
    );

    QUnit.test(
        "a missing contact never falls back to conversation-linked records",
        async (assert) => {
            const model = modelFor(async () =>
                page([customerRecord(11)], {partner: false, commercial_partner: false})
            );
            await model.load();
            assert.notOk(model.state.partner);
            assert.deepEqual(model.page.items, []);
            assert.notOk(model.page.hasMore);
            assert.notOk(await model.openRecord(11));
        }
    );

    QUnit.test(
        "all tabs open the correct native model and refresh on close",
        async (assert) => {
            const actions = [];
            let reads = 0;
            const model = modelFor(
                async (_method, args) => {
                    reads += 1;
                    return responseFor(args[1]);
                },
                {
                    async doAction(action, options) {
                        actions.push({action, options});
                    },
                }
            );
            await model.load();
            for (const tab of CUSTOMER_TABS) {
                await model.selectTab(tab.id);
                assert.notOk(await model.openRecord(999));
                assert.ok(await model.openRecord(11));
                const opened = actions[actions.length - 1];
                assert.strictEqual(opened.action.res_model, tab.model);
                assert.strictEqual(opened.action.res_id, 11);
                assert.strictEqual(opened.action.target, "new");
                const before = reads;
                await opened.options.onClose();
                assert.strictEqual(reads, before + 1);
            }
            const before = reads;
            model.destroy();
            await actions[0].options.onClose();
            assert.strictEqual(reads, before);
        }
    );

    QUnit.test(
        "closing an old record does not reload the newly selected tab",
        async (assert) => {
            let close = false;
            let reads = 0;
            const model = modelFor(
                async (_method, args) => {
                    reads += 1;
                    return responseFor(args[1]);
                },
                {
                    async doAction(_action, options) {
                        close = options.onClose;
                    },
                }
            );
            await model.load();
            await model.openRecord(11);
            await model.selectTab("orders");
            const before = reads;
            assert.notOk(await close());
            assert.strictEqual(reads, before);
        }
    );

    QUnit.test(
        "native form failure keeps the current customer panel usable",
        async (assert) => {
            const model = modelFor(async () => page(), {
                async doAction() {
                    throw new Error("Form unavailable");
                },
            });
            await model.load();
            assert.notOk(await model.openRecord(11));
            assert.ok(model.state.operationError);
            assert.strictEqual(model.page.phase, "ready");
            assert.strictEqual(model.page.items.length, 2);
        }
    );

    QUnit.test(
        "renders four tabs, customer records and no conversation link controls",
        async (assert) => {
            const calls = [];
            let closed = false;
            const store = modelFor(async (method, args) => {
                calls.push(method);
                return responseFor(args[1]);
            }).store;
            const target = document.createElement("div");
            target.className = "o_contact_center_ui cc-details-open";
            getFixture().appendChild(target);
            await mount(CrmPanel, target, {
                env: {services: {action: {doAction: () => Promise.resolve()}}},
                props: {
                    store,
                    channelId: 404,
                    onClose: () => {
                        closed = true;
                    },
                },
            });
            await nextTick();
            assert.strictEqual(
                target.querySelector("aside").getAttribute("aria-label"),
                "Comercial do cliente"
            );
            assert.strictEqual(target.querySelectorAll('[role="tab"]').length, 4);
            assert.ok(target.textContent.includes("Pessoa de teste"));
            assert.ok(target.textContent.includes("Empresa de teste"));
            assert.ok(target.textContent.includes("Sem vendedor"));
            assert.notOk(target.textContent.includes("Vinculada à conversa"));
            assert.notOk(target.textContent.includes("Vincular à conversa"));
            assert.notOk(target.textContent.includes("Desvincular conversa"));
            assert.strictEqual(
                document.activeElement.getAttribute("aria-label"),
                "Fechar comercial do cliente"
            );
            await click(target, "#cc-crm-tab-orders");
            await nextTick();
            assert.strictEqual(
                target
                    .querySelector("#cc-crm-tab-orders")
                    .getAttribute("aria-selected"),
                "true"
            );
            assert.notOk(target.textContent.includes("Sem vendedor"));
            assert.ok(calls.every((method) => method === "get_customer_records"));
            await click(target, '[aria-label="Fechar comercial do cliente"]');
            assert.ok(closed);
        }
    );

    QUnit.test(
        "unavailable tabs remain visible and explain missing access",
        async (assert) => {
            const tabs = CUSTOMER_TABS.map(({id}) => ({
                id,
                available: id === "opportunities",
            }));
            const store = modelFor(async () => page(undefined, {tabs})).store;
            const target = getFixture();
            await mount(CrmPanel, target, {
                env: {services: {action: {doAction: () => Promise.resolve()}}},
                props: {store, channelId: 404, onClose: () => false},
            });
            await nextTick();
            await click(target, "#cc-crm-tab-invoices");
            await nextTick();
            assert.strictEqual(target.querySelectorAll('[role="tab"]').length, 4);
            assert.strictEqual(
                target.querySelectorAll(".cc-customer-record").length,
                0
            );
            assert.ok(target.textContent.includes("Esta aba está indisponível"));
            assert.ok(target.textContent.includes("não tem acesso"));
        }
    );
});
