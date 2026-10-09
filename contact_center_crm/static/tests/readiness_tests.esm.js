/** @odoo-module **/
/* global QUnit */

import "@contact_center_crm/js/contact_center_app_crm.esm";
import {
    CUSTOMER_TABS,
    CrmPanel,
    CrmPanelModel,
} from "@contact_center_crm/js/crm_panel.esm";
import {Component, reactive, useState, xml} from "@odoo/owl";
import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";
import {ContactCenterStore} from "@contact_center_ui/js/contact_center_store.esm";
import {getFixture, makeDeferred, mount, nextTick} from "@web/../tests/helpers/utils";
import {makeFakeLocalizationService} from "@web/../tests/helpers/mock_services";

function compact() {
    return {
        projection: "list_v1",
        channel_id: 404,
        conversation_type: "direct",
        name: "Pessoa",
        state: "open",
        unread_count: 1,
        responsible: {id: 9, name: "Agente"},
        preference: {},
        tags: [],
        account: {id: 1, name: "Comercial", platform: "whatsapp"},
        identity: {id: 31, name: "Pessoa", avatar_url: false},
        group: false,
        capabilities: {delete_conversation: true, ignore_conversation: true},
    };
}

function full() {
    const row = compact();
    delete row.projection;
    return {
        ...row,
        can_send: true,
        identity: {
            ...row.identity,
            partner: {id: 21, name: "Pessoa", company: {id: 22, name: "Empresa"}},
            aliases: [{id: 7, value: "alias"}],
        },
        capabilities: {...row.capabilities, view_attribution: true},
    };
}

function customerPage() {
    return {
        schema_version: 1,
        channel_id: 404,
        tab: "opportunities",
        status: "ready",
        partner: {id: 21, name: "Pessoa"},
        commercial_partner: {id: 22, name: "Empresa"},
        tabs: CUSTOMER_TABS.map((tab) => ({id: tab.id, available: true})),
        items: [],
        has_more: false,
    };
}

class EmptyComponent extends Component {}
EmptyComponent.template = xml`<span/>`;
EmptyComponent.props = ["*"];

QUnit.module("contact_center_crm > authorized detail lifecycle", (hooks) => {
    hooks.beforeEach(() => {
        makeFakeLocalizationService();
        const banner = document.getElementById("oe_neutralize_banner");
        if (banner) {
            (banner.parentElement || banner).remove();
        }
    });

    QUnit.test(
        "pending compact detail never admits a CRM read or customer operation",
        async (assert) => {
            const calls = [];
            const store = {
                state: {selectedChannelId: 404},
                permissionReady: false,
                selectedConversation: compact(),
                capabilities: {view_crm: true},
                call: (...args) => {
                    calls.push(args);
                    return customerPage();
                },
            };
            const model = new CrmPanelModel({
                store,
                channelId: 404,
                action: {doAction: () => true},
            });
            assert.notOk(await model.load());
            assert.deepEqual(calls, [], "no first-detail extension read");
            model.destroy();
            store.selectedConversation = full();
            store.permissionReady = true;
            // Production mounts the panel only after ready, binding its model
            // to the authorized customer's key at that point.
            const readyModel = new CrmPanelModel({
                store,
                channelId: 404,
                action: {doAction: () => true},
            });
            assert.ok(await readyModel.load());
            assert.strictEqual(calls.length, 1);
            store.permissionReady = false;
            assert.notOk(
                await readyModel.load(),
                "a current denial stops extension reads"
            );
            assert.strictEqual(calls.length, 1, "denial admits no new customer read");
            readyModel.destroy();
        }
    );

    QUnit.test(
        "real CRM panel mounts once after ready and stays mounted through list and revalidation",
        async (assert) => {
            const calls = [];
            const panels = [];
            let detailResponse = false;
            const rpc = (_model, method) => {
                calls.push(method);
                if (method === "get_customer_records") {
                    return Promise.resolve(customerPage());
                }
                if (method === "get_conversation") {
                    return detailResponse;
                }
                throw new Error(`Unexpected test RPC ${method}`);
            };
            const store = new ContactCenterStore({
                orm: {call: rpc, silent: {call: rpc}},
                busService: new EventTarget(),
                stateFactory: reactive,
                notification: false,
            });
            store.state.phase = "ready";
            store.state.bootstrap = {user: {id: 9}, capabilities: {view_crm: true}};
            store.state.conversations = [compact()];
            store.state.selectedChannelId = 404;
            store.state.detailsOpen = true;
            Object.assign(store.state.selectedDetail, {
                channelId: 404,
                status: "loading",
            });

            class TrackedCrmPanel extends CrmPanel {
                setup() {
                    super.setup();
                    panels.push(this);
                }
            }
            class ReadyApp extends ContactCenterApp {
                setup() {
                    this.store = this.props.store;
                    this.store.state = useState(this.store.state);
                    this.ui = useState({sidePanel: "crm", stateChanging: false});
                }
            }
            ReadyApp.props = {store: Object};
            ReadyApp.components = {
                ...ContactCenterApp.components,
                CrmPanel: TrackedCrmPanel,
                ConversationList: EmptyComponent,
                ContactPanel: EmptyComponent,
                ConversationTimeline: EmptyComponent,
                MessageComposer: EmptyComponent,
                ConversationTags: EmptyComponent,
            };
            const target = getFixture();
            try {
                const app = await mount(ReadyApp, target, {
                    env: {services: {action: {doAction: () => Promise.resolve()}}},
                    props: {store},
                });
                assert.notOk(target.querySelector("#cc-crm-panel"));
                assert.deepEqual(
                    calls,
                    [],
                    "first loading does not mount the extension"
                );
                store.state.selectedDetail.status = "error";
                await nextTick();
                assert.notOk(target.querySelector("#cc-crm-panel"));
                store.replaceConversation(full(), {insert: true});
                await nextTick();
                await nextTick();
                const panelElement = target.querySelector("#cc-crm-panel");
                const panel = panels[0];
                assert.ok(panelElement);
                assert.strictEqual(panels.length, 1);
                assert.strictEqual(
                    calls.filter((method) => method === "get_customer_records").length,
                    1
                );
                const key = app.crmPanelKey;
                panel.model.page.query = "rascunho de busca";
                panel.state.operationStatus = "Estado local preservado";
                store.state.contactLinker.open = true;
                store.state.attribution.phase = "ready";
                store.replaceConversation({...compact(), name: "Nome recente"});
                await nextTick();
                assert.strictEqual(
                    app.crmPanelKey,
                    key,
                    "compact identity preserves the full customer key"
                );
                assert.strictEqual(target.querySelector("#cc-crm-panel"), panelElement);
                assert.strictEqual(panel.model.page.query, "rascunho de busca");
                assert.ok(store.state.contactLinker.open);
                assert.strictEqual(store.state.attribution.phase, "ready");

                const revalidation = makeDeferred();
                detailResponse = revalidation;
                const refresh = store.refreshSelectedConversation({silent: true});
                await nextTick();
                assert.ok(
                    store.permissionReady,
                    "R3 retains the previously authorized permissions"
                );
                assert.strictEqual(app.crmPanelKey, key);
                assert.strictEqual(target.querySelector("#cc-crm-panel"), panelElement);
                revalidation.resolve({schema_version: 1, item: full()});
                await refresh;
                await nextTick();
                assert.strictEqual(
                    panels.length,
                    1,
                    "revalidation does not remount the real panel"
                );
                assert.strictEqual(
                    panel.state.operationStatus,
                    "Estado local preservado"
                );
                assert.strictEqual(
                    calls.filter((method) => method === "get_customer_records").length,
                    1
                );

                const denied = makeDeferred();
                detailResponse = denied;
                const denialRefresh = store.refreshSelectedConversation({silent: true});
                denied.reject({data: {name: "odoo.exceptions.AccessError"}});
                await denialRefresh;
                await nextTick();
                assert.notOk(store.permissionReady);
                assert.notOk(
                    target.querySelector("#cc-crm-panel"),
                    "effective denial removes the panel"
                );
                assert.ok(panel.model.destroyed);
            } finally {
                store.destroy();
            }
        }
    );
});
