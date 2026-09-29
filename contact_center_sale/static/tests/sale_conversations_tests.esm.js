/** @odoo-module **/
/* global QUnit */

import "@contact_center_sale/js/sale_conversations.esm";
import {
    nextAnimationFrame,
    start,
    startServer,
} from "@mail/../tests/helpers/test_utils";
import {SUPPORTED_SCHEMA_VERSION} from "@contact_center_ui/js/contact_center_model.esm";
import {addFakeModel} from "@bus/../tests/helpers/model_definitions_helpers";
import {makeDeferred} from "@web/../tests/helpers/utils";

// The headless Odoo runner normally logs assertion totals only per module.
// Emit case completion receipts so the local proof cannot pass with stale assets.
QUnit.testDone(({module, name, failed}) => {
    if (module.startsWith("contact_center_sale >")) {
        console.log(`CC_SALE_CASE ${JSON.stringify({name, failed})}`);
    }
});

const envelope = (data) => ({schema_version: SUPPORTED_SCHEMA_VERSION, ...data});
const row = (id = 91, allowed = true) => ({
    channel_id: allowed ? id : false,
    contact_name: "Bruno",
    inbox_name: allowed ? "Comercial" : "Alice",
    can_open: allowed,
});
const conversation = (id = 91) => ({
    channel_id: id,
    name: "Bruno",
    conversation_type: "direct",
    state: "open",
    can_send: false,
    capabilities: {},
    account: {id: 1, name: "Comercial"},
    unread_count: 0,
});

addFakeModel("sale.order", {name: {string: "Order", type: "char"}});

async function setup(options = {}) {
    const pyEnv = await startServer({
        views: {
            "sale.order,false,form":
                '<form><sheet><button name="action_contact_center_conversations" type="object" string="Atendimentos"/><field name="name"/></sheet></form>',
        },
    });
    const orderId = pyEnv["sale.order"].create({name: "PV de teste"});
    const config = {
        items: [row(), row(92, false)],
        listError: false,
        openError: false,
        ...options,
    };
    const calls = [];
    const result = await start({
        mockRPC(route, args) {
            if (args.model === "sale.order") {
                calls.push(args);
                if (args.method === "action_contact_center_conversations") {
                    return {
                        type: "ir.actions.client",
                        tag: "contact_center_sale.conversations",
                        params: {order_id: orderId},
                    };
                }
                if (args.method === "get_contact_center_conversations") {
                    if (config.listError) {
                        throw new Error("Offline");
                    }
                    const offset = args.kwargs.offset || 0;
                    return {
                        items: config.items.slice(offset, offset + 50),
                        total: config.items.length,
                        offset,
                        limit: 50,
                        has_more: offset + 50 < config.items.length,
                    };
                }
                if (args.method === "open_contact_center_conversation") {
                    if (config.openError) {
                        config.items = [row(91, false)];
                        throw new Error("Access revoked");
                    }
                    return config.openDeferred
                        ? config.openDeferred
                        : envelope({item: conversation(args.args[1])});
                }
            }
            if (args.model === "contact.center.ui.api") {
                calls.push(args);
                switch (args.method) {
                    case "systray_summary":
                        return envelope({enabled: false});
                    case "bootstrap":
                        return envelope({
                            user: {id: 3},
                            accounts: [],
                            agents: [],
                            tags: [],
                            capabilities: {},
                        });
                    case "get_conversation":
                        return envelope({item: conversation(args.args[0])});
                    case "get_timeline":
                        return envelope({
                            channel_id: args.args[0],
                            items: [],
                            has_more: false,
                        });
                    default:
                        throw new Error(`Unexpected CC RPC: ${args.method}`);
                }
            }
        },
    });
    await result.openView({
        res_model: "sale.order",
        res_id: orderId,
        views: [[false, "form"]],
    });
    const open = async () => {
        await result.env.services.action.doAction({
            type: "ir.actions.client",
            tag: "contact_center_sale.conversations",
            params: {order_id: orderId},
        });
        await nextAnimationFrame();
    };
    return {...result, config, calls, open, orderId};
}

QUnit.module("contact_center_sale > sales conversations", () => {
    QUnit.test(
        "single conversation opens floating chat and reuses it without leaving sales",
        async (assert) => {
            const {open, calls, orderId} = await setup({items: [row()]});
            const location = window.location.href;
            await open();
            assert.containsNone(document.body, ".cc-sale-conversations");
            assert.containsOnce(
                document.body,
                '.o_ChatWindow[data-contact-center-id="91"]'
            );
            assert.containsOnce(document.body, ".o_form_view");
            assert.strictEqual(window.location.href, location);
            await open();
            assert.containsOnce(
                document.body,
                '.o_ChatWindow[data-contact-center-id="91"]'
            );
            const selected = calls.find(
                (c) => c.method === "open_contact_center_conversation"
            );
            assert.deepEqual(selected.args, [[orderId], 91]);
        }
    );

    QUnit.test(
        "multiple inboxes list names with restricted rows and open selected conversation",
        async (assert) => {
            const {open, click, calls} = await setup();
            const location = window.location.href;
            await open();
            assert.containsN(document.body, ".cc-sale-conversations tbody tr", 2);
            assert.containsOnce(document.body, ".cc-sale-restricted");
            assert.containsOnce(document.body, ".cc-sale-open");
            assert.notOk(calls.some((c) => c.method === "get_timeline"));
            await click(".cc-sale-open");
            assert.containsNone(document.body, ".cc-sale-conversations");
            assert.containsOnce(
                document.body,
                '.o_ChatWindow[data-contact-center-id="91"]'
            );
            assert.strictEqual(window.location.href, location);
        }
    );

    QUnit.test(
        "a sole restricted conversation shows the picker without an open button",
        async (assert) => {
            const {open, calls} = await setup({items: [row(91, false)]});
            await open();
            assert.containsOnce(document.body, ".cc-sale-restricted");
            assert.containsNone(document.body, ".cc-sale-open");
            assert.notOk(
                calls.some((c) => c.method === "open_contact_center_conversation")
            );
        }
    );

    QUnit.test("pagination visits every row and escapes names", async (assert) => {
        const items = Array.from({length: 51}, (_, i) => row(i + 91));
        items[50].contact_name = '<img src="x" onerror="alert(1)">';
        const {open, click} = await setup({items});
        await open();
        assert.containsN(document.body, ".cc-sale-conversations tbody tr", 50);
        await click(".cc-sale-next");
        assert.containsN(document.body, ".cc-sale-conversations tbody tr", 1);
        assert.containsNone(document.body, ".cc-sale-conversations img");
        assert.ok(
            document
                .querySelector(".cc-sale-conversations tbody")
                .textContent.includes("<img")
        );
        await click(".cc-sale-previous");
        assert.containsN(document.body, ".cc-sale-conversations tbody tr", 50);
    });

    QUnit.test(
        "empty and list failure states allow retry without stale rows",
        async (assert) => {
            const {open, click, config} = await setup({listError: true});
            await open();
            assert.containsOnce(document.body, '.cc-sale-conversations [role="alert"]');
            assert.containsNone(document.body, ".cc-sale-open");
            config.listError = false;
            config.items = [];
            await click(".cc-sale-retry");
            assert.containsOnce(document.body, ".cc-sale-empty");
            assert.containsNone(document.body, '.cc-sale-conversations [role="alert"]');
        }
    );

    QUnit.test(
        "revocation after direct selection refreshes the picker without opening content",
        async (assert) => {
            const {open, calls} = await setup({items: [row()], openError: true});
            await open();
            assert.containsOnce(document.body, ".cc-sale-restricted");
            assert.containsNone(document.body, ".cc-chat-content");
            assert.notOk(calls.some((c) => c.method === "get_timeline"));
        }
    );

    QUnit.test(
        "revocation in picker keeps it open and refreshes authorization",
        async (assert) => {
            const {open, click, calls} = await setup({openError: true});
            await open();
            await click(".cc-sale-open");
            assert.containsOnce(document.body, '.cc-sale-conversations [role="alert"]');
            assert.containsOnce(document.body, ".cc-sale-restricted");
            assert.containsNone(document.body, ".cc-chat-content");
            assert.notOk(calls.some((c) => c.method === "get_timeline"));
        }
    );

    QUnit.test(
        "double click opens once and closing during RPC never opens a late chat",
        async (assert) => {
            const deferred = makeDeferred();
            const {open, calls} = await setup({openDeferred: deferred});
            await open();
            document.querySelector(".cc-sale-open").click();
            document.querySelector(".cc-sale-open").click();
            await nextAnimationFrame();
            assert.strictEqual(
                calls.filter((c) => c.method === "open_contact_center_conversation")
                    .length,
                1
            );
            assert.ok(document.querySelector(".cc-sale-open").disabled);
            // Resolve in the same turn as close, before Owl's next render flush.
            document.querySelector(".cc-sale-close").click();
            deferred.resolve(envelope({item: conversation()}));
            await nextAnimationFrame();
            assert.containsNone(document.body, ".cc-sale-conversations");
            assert.containsNone(document.body, ".cc-chat-content");
        }
    );

    QUnit.test(
        "the real form button is reenabled while the picker remains open",
        async (assert) => {
            const {click} = await setup();
            await click('button[name="action_contact_center_conversations"]');
            assert.containsOnce(document.body, ".cc-sale-conversations");
            assert.notOk(
                document.querySelector(
                    'button[name="action_contact_center_conversations"]'
                ).disabled
            );
            assert.containsOnce(document.body, ".o_form_view");
        }
    );
});
