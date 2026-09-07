/** @odoo-module **/
/* global QUnit */

import "@contact_center_crm/js/contact_center_app_crm.esm";
import {
    CrmPanel,
    CrmPanelModel,
    normalizeCrmPage,
} from "@contact_center_crm/js/crm_panel.esm";
import {
    click,
    getFixture,
    makeDeferred,
    mount,
    nextTick,
} from "@web/../tests/helpers/utils";
import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";

function opportunity(id, linked = false) {
    return {
        id,
        name: `Oportunidade ${id}`,
        type: "opportunity",
        active: true,
        linked,
        stage: {id: 1, name: "Qualificação", is_won: false},
        team: {id: 1, name: "Comercial"},
        user: false,
        expected_revenue: 1000,
        currency: {id: 1, symbol: "R$", position: "before"},
    };
}

function page(items = [opportunity(11, true), opportunity(12)], overrides = {}) {
    return {
        schema_version: 1,
        channel_id: 404,
        partner: {id: 21, name: "Pessoa de teste"},
        commercial_partner: {id: 22, name: "Empresa de teste"},
        capabilities: {view: true, link: true, unlink: true},
        linked_opportunity_ids: items
            .filter((item) => item.linked)
            .map((item) => item.id),
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
    };
    return new CrmPanelModel({store, channelId: 404, action});
}

QUnit.module("contact_center_crm > opportunities", (hooks) => {
    hooks.beforeEach(() => {
        const banner = document.getElementById("oe_neutralize_banner");
        if (banner) {
            (banner.parentElement || banner).remove();
        }
    });

    QUnit.test(
        "normalizes only the current conversation and explicit permissions",
        (assert) => {
            const projection = normalizeCrmPage(page(), 404);
            assert.strictEqual(projection.items[0].linked, true);
            assert.strictEqual(projection.items[0].user, false);
            assert.strictEqual(projection.partner.id, 21);
            assert.strictEqual(projection.company.id, 22);
            const archived = normalizeCrmPage(
                page([{...opportunity(13, true), type: "lead", active: false}]),
                404
            ).items[0];
            assert.strictEqual(archived.type, "lead");
            assert.notOk(archived.active);
            assert.throws(() => normalizeCrmPage(page(), 405));
            assert.throws(() => normalizeCrmPage(page([], {schema_version: 2}), 404));
            assert.throws(() =>
                normalizeCrmPage(page([{id: 11, name: "No link state"}]), 404)
            );
            assert.throws(() => normalizeCrmPage(page([], {has_more: true}), 404));
            assert.deepEqual(
                normalizeCrmPage(page(undefined, {capabilities: {view: "true"}}), 404)
                    .items,
                [],
                "malformed permission cannot expose opportunities"
            );
        }
    );

    QUnit.test(
        "capability controls CRM access without any Kanban state",
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
            assert.deepEqual(calls, [["get_crm_opportunities", [404, "", 0, 20]]]);
            assert.strictEqual(model.state.phase, "ready");
        }
    );

    QUnit.test("CRM and contact controls select one side panel", (assert) => {
        const app = Object.create(ContactCenterApp.prototype);
        app.crmUi = {panelMode: "contact"};
        app.store = {
            capabilities: {view_crm: true},
            state: {detailsOpen: true},
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
        app.store.capabilities.view_crm = false;
        app.toggleCrmPanel();
        assert.notOk(app.store.state.detailsOpen);
    });

    QUnit.test("a changed conversation discards the old response", async (assert) => {
        const pending = makeDeferred();
        const model = modelFor(() => pending);
        const load = model.load();
        model.store.state.selectedChannelId = 405;
        pending.resolve(page());
        assert.notOk(await load);
        assert.deepEqual(model.state.items, []);
    });

    QUnit.test(
        "close and reopen cannot reuse a previous panel response",
        async (assert) => {
            const pending = makeDeferred();
            const previous = modelFor(() => pending);
            const load = previous.load();
            previous.destroy();
            const current = modelFor(async () => page([opportunity(13)]));
            await current.load();
            pending.resolve(page());
            assert.notOk(await load);
            assert.deepEqual(
                current.state.items.map((item) => item.id),
                [13]
            );
        }
    );

    QUnit.test("new searches win over slow previous searches", async (assert) => {
        const pending = makeDeferred();
        let count = 0;
        const model = modelFor(() => {
            count += 1;
            return count === 1 ? pending : Promise.resolve(page([opportunity(13)]));
        });
        const oldLoad = model.load();
        model.state.query = "Novo";
        await model.load();
        pending.resolve(page());
        assert.notOk(await oldLoad);
        assert.deepEqual(
            model.state.items.map((item) => item.id),
            [13]
        );
    });

    QUnit.test(
        "pagination appends safe unique items with the same search",
        async (assert) => {
            const calls = [];
            const model = modelFor(async (_method, args) => {
                calls.push(args);
                return args[2]
                    ? page([opportunity(11), opportunity(12)])
                    : page([opportunity(11)], {has_more: true});
            });
            model.state.query = "  Solar  ";
            await model.load();
            model.state.query = "Busca ainda não enviada";
            await model.load({append: true});
            assert.deepEqual(calls, [
                [404, "Solar", 0, 20],
                [404, "Solar", 1, 20],
            ]);
            assert.deepEqual(
                model.state.items.map((item) => item.id),
                [11, 12]
            );
            assert.notOk(model.state.hasMore);
        }
    );

    QUnit.test(
        "loading errors can retry and denied access clears every projection",
        async (assert) => {
            let mode = "error";
            const model = modelFor(async () => {
                if (mode === "error") {
                    throw new Error("Network");
                }
                if (mode === "denied") {
                    throw Object.assign(new Error("Access denied"), {
                        data: {name: "odoo.exceptions.AccessError"},
                    });
                }
                return page();
            });
            assert.notOk(await model.load());
            assert.strictEqual(model.state.phase, "error");
            mode = "ready";
            assert.ok(await model.load());
            mode = "denied";
            assert.notOk(await model.load());
            assert.strictEqual(model.state.phase, "denied");
            assert.deepEqual(model.state.items, []);
            assert.notOk(model.state.partner);
            assert.notOk(model.state.company);
            assert.notOk(model.state.capabilities.link);
        }
    );

    QUnit.test(
        "existing conversation links remain visible without a current contact",
        async (assert) => {
            const model = modelFor(async () =>
                page([opportunity(11, true)], {
                    partner: false,
                    commercial_partner: false,
                    capabilities: {view: true, link: false, unlink: true},
                })
            );
            await model.load();
            assert.notOk(model.state.partner);
            assert.ok(model.state.items[0].linked);
            assert.ok(model.state.capabilities.unlink);
            assert.notOk(model.state.capabilities.link);
        }
    );

    QUnit.test(
        "link and unlink use visible opportunity IDs and reload without duplicates",
        async (assert) => {
            let linked = false;
            const calls = [];
            const model = modelFor(async (method, args) => {
                calls.push({method, args});
                if (method === "get_crm_opportunities") {
                    return page([opportunity(12, linked)]);
                }
                linked = method === "link_crm_opportunity";
                return {schema_version: 1, channel_id: 404, opportunity_id: 12, linked};
            });
            await model.load();
            assert.notOk(await model.setLinked(999, true));
            assert.ok(await model.setLinked(12, true));
            assert.ok(model.state.items[0].linked);
            assert.notOk(await model.setLinked(12, true));
            assert.ok(await model.setLinked(12, false));
            assert.notOk(model.state.items[0].linked);
            assert.deepEqual(
                calls.filter((call) => call.method !== "get_crm_opportunities"),
                [
                    {method: "link_crm_opportunity", args: [404, 12]},
                    {method: "unlink_crm_opportunity", args: [404, 12]},
                ]
            );
        }
    );

    QUnit.test(
        "an ambiguous write is read back and is never automatically repeated",
        async (assert) => {
            let linked = false;
            let writes = 0;
            const model = modelFor(async (method) => {
                if (method === "get_crm_opportunities") {
                    return page([opportunity(12, linked)]);
                }
                writes += 1;
                linked = true;
                throw new Error("Response lost after commit");
            });
            await model.load();
            assert.notOk(await model.setLinked(12, true));
            assert.strictEqual(writes, 1);
            assert.ok(model.state.items[0].linked);
            assert.notOk(model.state.busyId);
            assert.ok(model.state.operationError);
        }
    );

    QUnit.test(
        "a pending write keeps its original target after changing conversation",
        async (assert) => {
            const pending = makeDeferred();
            const calls = [];
            const model = modelFor(async (method, args) => {
                calls.push({method, args});
                return method === "get_crm_opportunities" ? page() : pending;
            });
            await model.load();
            const mutation = model.setLinked(12, true);
            assert.notOk(await model.setLinked(12, true), "double click is ignored");
            model.store.state.selectedChannelId = 405;
            pending.resolve({
                schema_version: 1,
                channel_id: 404,
                opportunity_id: 12,
                linked: true,
            });
            await mutation;
            assert.deepEqual(calls[calls.length - 1], {
                method: "link_crm_opportunity",
                args: [404, 12],
            });
            assert.strictEqual(
                calls.length,
                2,
                "no refresh is issued for the new conversation"
            );
        }
    );

    QUnit.test(
        "opening a visible opportunity uses native form and refreshes on close",
        async (assert) => {
            const actions = [];
            let reads = 0;
            const model = modelFor(
                async () => {
                    reads += 1;
                    return page();
                },
                {
                    async doAction(action, options) {
                        actions.push({action, options});
                    },
                }
            );
            await model.load();
            assert.notOk(await model.openOpportunity(999));
            assert.ok(await model.openOpportunity(11));
            assert.strictEqual(actions[0].action.res_model, "crm.lead");
            assert.strictEqual(actions[0].action.res_id, 11);
            assert.strictEqual(actions[0].action.target, "new");
            await actions[0].options.onClose();
            assert.strictEqual(reads, 2);
            model.destroy();
            await actions[0].options.onClose();
            assert.strictEqual(reads, 2, "closing a form cannot revive its old panel");
        }
    );

    QUnit.test(
        "renders customer, cards and accessible link actions",
        async (assert) => {
            let linked = false;
            let closed = false;
            const store = modelFor(async (method) => {
                if (method === "get_crm_opportunities") {
                    return page([opportunity(12, linked)]);
                }
                linked = true;
                return {schema_version: 1, channel_id: 404, opportunity_id: 12, linked};
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
                "Oportunidades do cliente"
            );
            assert.ok(target.textContent.includes("Pessoa de teste"));
            assert.ok(target.textContent.includes("Empresa de teste"));
            assert.ok(target.textContent.includes("Sem vendedor"));
            assert.notOk(target.textContent.includes("Service Cases"));
            assert.strictEqual(
                document.activeElement.getAttribute("aria-label"),
                "Fechar oportunidades"
            );
            await click(target, ".cc-crm-opportunity .cc-secondary-button");
            await nextTick();
            assert.ok(target.textContent.includes("Vinculada à conversa"));
            assert.ok(target.querySelector(".cc-crm-opportunity__unlink"));
            await click(target, "[aria-label='Fechar oportunidades']");
            assert.ok(closed);
        }
    );
});
