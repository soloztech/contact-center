/** @odoo-module **/
/* global QUnit */

import "@contact_center_crm/js/contact_center_app_crm.esm";
import {
    CUSTOMER_TABS,
    CrmPanel,
    CrmPanelModel,
    normalizeCustomerPage,
    normalizeSaleAttachments,
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

function saleRecord(id = 11, tab = "quotations", overrides = {}) {
    return customerRecord(id, tab, {
        document_tools: {
            pdf_url: `/contact_center/customer/404/sale/${id}/pdf`,
            attachments: true,
        },
        ...overrides,
    });
}

function saleAttachments(ids = [41], overrides = {}) {
    return {
        schema_version: 1,
        channel_id: 404,
        order_id: 11,
        items: ids.map((id) => ({
            id,
            name: `Documento ${id}.pdf`,
            mimetype: "application/pdf",
            size_bytes: 120,
            url: `/contact_center/customer/404/sale/11/attachment/${id}`,
        })),
        has_more: false,
        ...overrides,
    };
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
        "document links are scoped to the projected sale and channel",
        (assert) => {
            const result = normalizeCustomerPage(
                page([saleRecord()], {tab: "quotations", can_create_quotation: true}),
                404,
                "quotations"
            );
            assert.ok(result.canCreateQuotation);
            assert.strictEqual(
                result.items[0].documentTools.pdfUrl,
                "/contact_center/customer/404/sale/11/pdf"
            );
            for (const url of [
                "https://example.com/file.pdf",
                "/contact_center/customer/405/sale/11/pdf",
                "/contact_center/customer/404/sale/12/pdf",
                "/contact_center/customer/404/sale/11/pdf?redirect=1",
            ]) {
                const projection = normalizeCustomerPage(
                    page(
                        [
                            saleRecord(11, "orders", {
                                document_tools: {pdf_url: url, attachments: true},
                            }),
                        ],
                        {tab: "orders"}
                    ),
                    404,
                    "orders"
                );
                assert.notOk(projection.items[0].documentTools.pdfUrl);
            }
            assert.notOk(
                normalizeCustomerPage(page(), 404, "opportunities").canCreateQuotation
            );
            assert.notOk(
                normalizeCustomerPage(
                    page([], {partner: false, can_create_quotation: true}),
                    404,
                    "opportunities"
                ).canCreateQuotation
            );
        }
    );

    QUnit.test(
        "attachment metadata rejects another customer path and invalid pagination",
        (assert) => {
            const result = normalizeSaleAttachments(saleAttachments(), 404, 11);
            assert.strictEqual(result.items[0].sizeBytes, 120);
            for (const overrides of [
                {channel_id: 405},
                {order_id: 12},
                {items: [], has_more: true},
                {items: [{...saleAttachments().items[0], url: "/web/content/41"}]},
                {items: [{...saleAttachments().items[0], size_bytes: -1}]},
            ]) {
                assert.throws(() =>
                    normalizeSaleAttachments(saleAttachments([41], overrides), 404, 11)
                );
            }
        }
    );

    QUnit.test(
        "sale attachments load on demand and paginate independently of records",
        async (assert) => {
            const calls = [];
            const model = modelFor(async (method, args) => {
                calls.push({method, args});
                if (method === "get_customer_records") {
                    return page([saleRecord(), saleRecord(12)], {tab: args[1]});
                }
                return args[2]
                    ? saleAttachments([41, 42])
                    : saleAttachments([41], {has_more: true});
            });
            await model.selectTab("quotations");
            assert.strictEqual(
                calls.length,
                1,
                "opening tab does not fetch attachments or PDF"
            );
            await model.toggleAttachments(11);
            assert.deepEqual(calls[1], {
                method: "get_customer_sale_attachments",
                args: [404, 11, 0, 20],
            });
            assert.strictEqual(model.page.items[1].attachments.phase, "idle");
            await model.loadAttachments(11, {append: true});
            assert.deepEqual(calls[2].args, [404, 11, 1, 20]);
            assert.deepEqual(
                model.page.items[0].attachments.items.map((item) => item.id),
                [41, 42]
            );
            await model.toggleAttachments(11);
            await model.toggleAttachments(11);
            assert.strictEqual(calls.length, 3, "reopening uses the loaded list");
        }
    );

    QUnit.test(
        "late attachment responses cannot enter another tab, customer or panel",
        async (assert) => {
            for (const change of ["tab", "customer", "channel", "destroy", "reload"]) {
                const pending = makeDeferred();
                const model = modelFor(async (method, args) =>
                    method === "get_customer_records"
                        ? page([saleRecord(11, args[1])], {tab: args[1]})
                        : pending
                );
                await model.selectTab("quotations");
                const oldItem = model.page.items[0];
                const loading = model.toggleAttachments(11);
                assert.notOk(await model.loadAttachments(11), "duplicate read blocked");
                if (change === "tab") {
                    await model.selectTab("orders");
                }
                if (change === "customer") {
                    model.store.selectedConversation.identity.partner.id = 31;
                }
                if (change === "channel") {
                    model.store.state.selectedChannelId = 405;
                }
                if (change === "destroy") {
                    model.destroy();
                }
                if (change === "reload") {
                    await model.load();
                }
                pending.resolve(saleAttachments());
                assert.notOk(await loading, change);
                assert.deepEqual(oldItem.attachments.items, [], change);
            }
        }
    );

    QUnit.test(
        "failed attachment pagination clears stale files and can retry",
        async (assert) => {
            let fail = false;
            const model = modelFor(async (method, args) => {
                if (method === "get_customer_records") {
                    return page([saleRecord()], {tab: args[1]});
                }
                if (fail) {
                    throw new Error("Denied");
                }
                return saleAttachments([41], {has_more: true});
            });
            await model.selectTab("quotations");
            await model.toggleAttachments(11);
            fail = true;
            assert.notOk(await model.loadAttachments(11, {append: true}));
            const attachments = model.page.items[0].attachments;
            assert.deepEqual(attachments.items, []);
            assert.ok(attachments.error);
            assert.notOk(attachments.loadingMore);
            fail = false;
            assert.ok(await model.loadAttachments(11));
            assert.strictEqual(attachments.items.length, 1);
        }
    );

    QUnit.test(
        "PDF and listed files go only to the draft bridge with captured customer",
        async (assert) => {
            const calls = [];
            const pending = makeDeferred();
            const model = modelFor(async (method, args) =>
                method === "get_customer_records"
                    ? page([saleRecord()], {tab: args[1]})
                    : saleAttachments()
            );
            model.store.addDraftAttachment = async (source) => {
                calls.push(source);
                return calls.length === 1 ? pending : true;
            };
            await model.selectTab("quotations");
            const adding = model.attachDocument(11);
            assert.notOk(
                await model.attachDocument(11),
                "duplicate draft action blocked"
            );
            assert.deepEqual(calls[0], {
                channelId: 404,
                customerKey: "21:22",
                url: "/contact_center/customer/404/sale/11/pdf",
                name: "Registro 11.pdf",
                mimetype: "application/pdf",
            });
            pending.resolve(true);
            assert.ok(await adding);
            assert.ok(model.state.operationStatus.includes("rascunho"));
            assert.notOk(
                await model.attachDocument(11, 999),
                "unknown file is never submitted"
            );
            await model.toggleAttachments(11);
            assert.ok(await model.attachDocument(11, 41));
            assert.strictEqual(calls[1].sizeBytes, 120);
            assert.strictEqual(calls[1].name, "Documento 41.pdf");
            assert.strictEqual(
                calls[1].url,
                "/contact_center/customer/404/sale/11/attachment/41"
            );
        }
    );

    QUnit.test(
        "draft refusal, failure and stale completion release the busy state",
        async (assert) => {
            const model = modelFor(async (_method, args) =>
                page([saleRecord()], {tab: args[1]})
            );
            await model.selectTab("quotations");
            model.store.addDraftAttachment = async () => false;
            assert.notOk(await model.attachDocument(11));
            assert.notOk(
                model.state.operationError,
                "bridge explains refusal without a duplicate alert"
            );
            assert.notOk(model.state.draftBusy);
            model.store.addDraftAttachment = async () => {
                throw new Error("Upload failed");
            };
            assert.notOk(await model.attachDocument(11));
            assert.ok(model.state.operationError);
            assert.notOk(model.state.draftBusy);
            const pending = makeDeferred();
            model.store.addDraftAttachment = () => pending;
            const adding = model.attachDocument(11);
            await model.selectTab("orders");
            pending.resolve(true);
            await adding;
            assert.notOk(
                model.state.operationStatus,
                "completion does not overwrite another tab"
            );
            assert.notOk(model.state.draftBusy);
        }
    );

    QUnit.test(
        "new quotation opens the customer action and refreshes after the native form closes",
        async (assert) => {
            const calls = [];
            const actions = [];
            const nativeAction = {
                type: "ir.actions.act_window",
                res_model: "sale.order",
                target: "new",
                views: [[false, "form"]],
                context: {default_partner_id: 21},
            };
            const model = modelFor(
                async (method, args) => {
                    calls.push({method, args});
                    return method === "get_customer_records"
                        ? page([saleRecord()], {
                              tab: args[1],
                              can_create_quotation: true,
                          })
                        : {schema_version: 1, channel_id: 404, action: nativeAction};
                },
                {doAction: async (action, options) => actions.push({action, options})}
            );
            await model.selectTab("quotations");
            assert.ok(await model.createQuotation());
            assert.deepEqual(calls[1], {
                method: "get_customer_quotation_action",
                args: [404],
            });
            assert.deepEqual(actions[0].action, nativeAction);
            await actions[0].options.onClose();
            assert.strictEqual(calls.length, 3);
            model.destroy();
            assert.notOk(await actions[0].options.onClose());
        }
    );

    QUnit.test(
        "quotation creation rejects unavailable access, stale responses and unexpected actions",
        async (assert) => {
            const pending = makeDeferred();
            let response = pending;
            let opened = 0;
            const model = modelFor(
                async (method, args) =>
                    method === "get_customer_records"
                        ? page([saleRecord()], {
                              tab: args[1],
                              can_create_quotation: true,
                          })
                        : response,
                {
                    doAction: async () => {
                        opened += 1;
                    },
                }
            );
            assert.notOk(await model.createQuotation());
            await model.selectTab("quotations");
            const creating = model.createQuotation();
            assert.notOk(await model.createQuotation(), "duplicate action blocked");
            await model.selectTab("orders");
            pending.resolve({schema_version: 1, channel_id: 404, action: {}});
            assert.notOk(await creating);
            assert.strictEqual(opened, 0);
            assert.notOk(model.state.quotationBusy);
            await model.selectTab("quotations");
            response = {
                schema_version: 1,
                channel_id: 404,
                action: {
                    type: "ir.actions.act_window",
                    res_model: "account.move",
                    target: "new",
                },
            };
            assert.notOk(await model.createQuotation());
            assert.ok(model.state.operationError);
            assert.strictEqual(opened, 0);
        }
    );

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
            app.ui = {sidePanel: "contact"};
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
        "sale document controls stay lazy and prepare a draft from a listed attachment",
        async (assert) => {
            const calls = [];
            const drafts = [];
            const store = modelFor(async (method, args) => {
                calls.push(method);
                if (method === "get_customer_sale_attachments") {
                    return saleAttachments();
                }
                return args[1] === "opportunities"
                    ? page()
                    : page([saleRecord()], {
                          tab: args[1],
                          can_create_quotation: true,
                      });
            }).store;
            store.addDraftAttachment = async (source) => {
                drafts.push(source);
                return true;
            };
            const target = document.createElement("div");
            target.className = "o_contact_center_ui cc-details-open";
            getFixture().appendChild(target);
            await mount(CrmPanel, target, {
                env: {services: {action: {doAction: () => Promise.resolve()}}},
                props: {store, channelId: 404, onClose: () => false},
            });
            await nextTick();
            assert.notOk(target.querySelector(".cc-customer-record__tools"));
            await click(target, "#cc-crm-tab-quotations");
            await nextTick();
            assert.ok(target.querySelector(".cc-crm-panel__create"));
            assert.deepEqual(calls, ["get_customer_records", "get_customer_records"]);
            const pdf = target.querySelector(".cc-customer-record__tools a");
            assert.strictEqual(
                pdf.getAttribute("href"),
                "/contact_center/customer/404/sale/11/pdf"
            );
            assert.strictEqual(pdf.target, "_blank");
            assert.ok(pdf.rel.includes("noopener"));
            await click(target, ".cc-customer-record__tools button[aria-expanded]");
            await nextTick();
            assert.strictEqual(calls[2], "get_customer_sale_attachments");
            const attachment = target.querySelector(".cc-sale-attachments__actions a");
            assert.strictEqual(attachment.getAttribute("download"), "Documento 41.pdf");
            await click(target, ".cc-sale-attachments__actions button");
            await nextTick();
            assert.strictEqual(drafts.length, 1);
            assert.strictEqual(drafts[0].channelId, 404);
            assert.ok(target.textContent.includes("Revise o rascunho"));
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
