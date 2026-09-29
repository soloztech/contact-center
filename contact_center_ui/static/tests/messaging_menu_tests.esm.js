/** @odoo-module **/

/* global QUnit */

import {
    ContactCenterChat,
    openContactCenterChat,
} from "@contact_center_ui/js/contact_center_messaging.esm";
import {editInput, patchWithCleanup} from "@web/../tests/helpers/utils";
import {
    nextAnimationFrame,
    start,
    startServer,
} from "@mail/../tests/helpers/test_utils";
import {SUPPORTED_SCHEMA_VERSION} from "@contact_center_ui/js/contact_center_model.esm";

const envelope = (values) => ({schema_version: SUPPORTED_SCHEMA_VERSION, ...values});
const row = (id = 91) => ({
    channel_id: id,
    name: `Cliente ${id}`,
    conversation_type: "direct",
    state: "open",
    can_send: true,
    capabilities: {},
    responsible: {id: 3, name: "Agente"},
    account: {id: 1, name: "Comercial"},
    unread_count: 2,
    last_activity_at: "2026-09-29 12:00:00",
    last_message: {body_text: "Preciso de um orçamento", content_type: "text"},
});

const message = (id, body, direction = "inbound") => ({
    message_id: id,
    body_text: body,
    content_type: "text",
    date: "2026-09-29 12:00:00",
    direction,
    origin: direction === "inbound" ? "provider" : "agent",
    author: {type: "partner", id: 1, name: "Cliente de teste"},
    actions: {},
    media: [],
});

async function setup({enabled = true, listError = false, messages = []} = {}) {
    await startServer();
    const calls = [];
    const result = await start({
        mockRPC(route, args) {
            if (args.model !== "contact.center.ui.api") {
                return;
            }
            calls.push(args);
            switch (args.method) {
                case "systray_summary":
                    return envelope({enabled, mine_unread: 2, all_unread: 8});
                case "list_conversations":
                    if (listError) {
                        throw new Error("Access denied");
                    }
                    return envelope({items: [row()], total: 1, has_more: false});
                case "bootstrap":
                    return envelope({
                        user: {id: 3},
                        accounts: [{id: 1, name: "Comercial"}],
                        agents: [],
                        tags: [],
                        capabilities: {},
                    });
                case "get_conversation":
                    return envelope({item: row(args.args[0])});
                case "get_timeline":
                    return envelope({
                        channel_id: args.args[0],
                        items: messages,
                        has_more: false,
                    });
                case "mark_seen":
                    return envelope({
                        channel_id: args.args[0],
                        message_id: args.args[1],
                    });
                case "send_message":
                    return envelope({
                        channel_id: args.args[0],
                        client_request_id: args.args[3],
                        message: message(902, args.args[1], "outbound"),
                    });
                default:
                    throw new Error(`Unexpected Contact Center call: ${args.method}`);
            }
        },
    });
    return {...result, calls};
}

QUnit.module("contact_center_ui > native messaging", () => {
    QUnit.test(
        "native menu contains the personal tab, with no separate shortcut",
        async (assert) => {
            const {click, calls} = await setup();
            assert.containsNone(document.body, ".o_contact_center_systray");
            await click(".o_MessagingMenu_toggler");
            assert.containsOnce(document.body, ".cc-messaging-tab");
            assert.containsOnce(
                document.body,
                '.o_MessagingMenuTab[data-tab-id="chat"]'
            );
            await click(".cc-messaging-tab");
            assert.containsOnce(document.body, ".cc-messaging-item.has-unread");
            assert.containsNone(document.body, ".o_MessagingMenu_notificationList");
            assert.strictEqual(
                document.querySelector(".cc-messaging-preview").textContent,
                "Preciso de um orçamento"
            );
            assert.strictEqual(
                getComputedStyle(document.querySelector(".cc-messaging-preview"))
                    .fontWeight,
                "750"
            );
            const list = calls.find((call) => call.method === "list_conversations");
            assert.deepEqual(list.kwargs.filters, {responsibility: "mine"});
            assert.notOk(
                calls.some((call) => call.method === "mark_seen"),
                "previews never mark a conversation read"
            );
            await click('.o_MessagingMenuTab[data-tab-id="chat"]');
            assert.containsNone(document.body, ".cc-messaging-list");
            assert.containsOnce(document.body, ".o_MessagingMenu_notificationList");
        }
    );

    QUnit.test(
        "click opens a native floating window without changing the action",
        async (assert) => {
            const {click, messaging, calls} = await setup();
            const location = window.location.href;
            await click(".o_MessagingMenu_toggler");
            await click(".cc-messaging-tab");
            await click(".cc-messaging-item");
            assert.containsOnce(
                document.body,
                '.o_ChatWindow[data-contact-center-id="91"]'
            );
            assert.containsOnce(
                document.body,
                ".cc-chat-content .cc-composer textarea"
            );
            assert.containsNone(document.body, ".o_ChatWindow_newMessageForm");
            assert.containsNone(document.body, ".o_ChatWindow_thread");
            assert.strictEqual(window.location.href, location);
            assert.ok(
                calls.some(
                    (call) => call.method === "get_timeline" && call.args[0] === 91
                )
            );
            const chatWindow = messaging.chatWindowManager.chatWindows.find(
                (win) => win.contactCenterChannelId === 91
            );
            assert.notOk(
                chatWindow.thread,
                "no native outbound mail channel is created"
            );
            openContactCenterChat(messaging, row());
            await nextAnimationFrame();
            assert.containsOnce(
                document.body,
                '.o_ChatWindow[data-contact-center-id="91"]',
                "reopening reuses the same window"
            );
            await click(".o_ChatWindowHeader");
            assert.ok(chatWindow.isFolded);
            assert.strictEqual(
                getComputedStyle(document.querySelector(".cc-chat-content")).display,
                "none"
            );
            await click(".o_ChatWindowHeader");
            assert.notOk(chatWindow.isFolded);
            await click(".o_ChatWindowHeader_commandClose");
            assert.containsNone(document.body, ".cc-chat-content");
            assert.strictEqual(window.location.href, location);
        }
    );

    QUnit.test(
        "users without Contact Center access keep only the native tabs",
        async (assert) => {
            const {click, calls} = await setup({enabled: false});
            await click(".o_MessagingMenu_toggler");
            assert.containsNone(document.body, ".cc-messaging-tab");
            assert.containsOnce(
                document.body,
                '.o_MessagingMenuTab[data-tab-id="all"]'
            );
            assert.notOk(calls.some((call) => call.method === "list_conversations"));
        }
    );

    QUnit.test(
        "list errors show a retry and no stale personal preview",
        async (assert) => {
            const {click} = await setup({listError: true});
            await click(".o_MessagingMenu_toggler");
            await click(".cc-messaging-tab");
            assert.containsOnce(document.body, '.cc-messaging-list [role="alert"]');
            assert.containsNone(document.body, ".cc-messaging-item");
        }
    );

    QUnit.test("window close respects active uploads and sends", (assert) => {
        const notices = [];
        const store = {
            state: {selectedChannelId: 91},
            isSending: () => false,
            conversationSelectionGuard: () => false,
            notify: (message) => notices.push(message),
        };
        assert.notOk(ContactCenterChat.prototype.canClose.call({store}));
        store.conversationSelectionGuard = () => true;
        patchWithCleanup(store, {isSending: () => true});
        assert.notOk(ContactCenterChat.prototype.canClose.call({store}));
        assert.strictEqual(notices.length, 2);
    });

    QUnit.test(
        "Contact Center stays floating while the underlying action is Discuss",
        async (assert) => {
            const {click, messaging, openDiscuss} = await setup();
            await openDiscuss({waitUntilMessagesLoaded: false});
            await click(".o_MessagingMenu_toggler");
            await click(".cc-messaging-tab");
            await click(".cc-messaging-item");
            assert.ok(messaging.discuss.discussView, "Discuss was not replaced");
            const chatWindow = messaging.chatWindowManager.chatWindows.find(
                (win) => win.contactCenterChannelId === 91
            );
            assert.ok(chatWindow.isVisible);
            assert.containsOnce(document.body, ".cc-chat-content textarea");
        }
    );

    QUnit.test(
        "the floating composer sends through the Contact Center API",
        async (assert) => {
            const {click, calls} = await setup({
                messages: [message(901, "Olá, preciso de ajuda")],
            });
            await click(".o_MessagingMenu_toggler");
            await click(".cc-messaging-tab");
            await click(".cc-messaging-item");
            assert.ok(
                document
                    .querySelector(".cc-timeline")
                    .textContent.includes("Olá, preciso de ajuda")
            );
            await editInput(
                document.body,
                ".cc-composer textarea",
                "Vamos ajudar por aqui."
            );
            await click(".cc-send-button");
            const send = calls.filter((call) => call.method === "send_message");
            assert.strictEqual(send.length, 1);
            assert.deepEqual(send[0].args.slice(0, 3), [
                91,
                "Vamos ajudar por aqui.",
                false,
            ]);
            assert.ok(
                send[0].args[3],
                "uses the same idempotent request identifier as the inbox"
            );
            assert.strictEqual(
                document.querySelector(".cc-composer textarea").value,
                ""
            );
            assert.ok(
                document
                    .querySelector(".cc-timeline")
                    .textContent.includes("Vamos ajudar por aqui.")
            );
        }
    );

    QUnit.test(
        "folding keeps the draft and multiple conversations use native placement",
        async (assert) => {
            const {click, messaging} = await setup();
            await click(".o_MessagingMenu_toggler");
            await click(".cc-messaging-tab");
            await click(".cc-messaging-item");
            await editInput(
                document.body,
                ".cc-composer textarea",
                "Rascunho preservado"
            );
            await click(".o_ChatWindowHeader");
            await click(".o_ChatWindowHeader");
            assert.strictEqual(
                document.querySelector(".cc-composer textarea").value,
                "Rascunho preservado"
            );
            openContactCenterChat(messaging, row(92));
            await nextAnimationFrame();
            await nextAnimationFrame();
            const windows = messaging.chatWindowManager.chatWindows.filter(
                (win) => win.contactCenterChannelId
            );
            assert.strictEqual(windows.length, 2);
            assert.notEqual(windows[0].visibleOffset, windows[1].visibleOffset);
            assert.strictEqual(
                document.querySelector('[data-contact-center-id="91"] textarea').value,
                "Rascunho preservado"
            );
        }
    );
});
