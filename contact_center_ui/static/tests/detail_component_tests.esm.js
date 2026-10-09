/** @odoo-module **/
/* global QUnit */

import {Component, reactive, useState, xml} from "@odoo/owl";
import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";
import {ContactCenterChat} from "@contact_center_ui/js/contact_center_messaging.esm";
import {ContactCenterStore} from "@contact_center_ui/js/contact_center_store.esm";
import {ConversationMenu} from "@contact_center_ui/js/conversation_list.esm";
import {ConversationTimeline} from "@contact_center_ui/js/conversation_timeline.esm";
import {MessageComposer} from "@contact_center_ui/js/message_composer.esm";
import {click, getFixture, mount, nextTick} from "@web/../tests/helpers/utils";
import {hotkeyService} from "@web/core/hotkeys/hotkey_service";
import {makeFakeLocalizationService} from "@web/../tests/helpers/mock_services";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";
import {registry} from "@web/core/registry";
import {uiService} from "@web/core/ui/ui_service";

function compactRow() {
    return {
        projection: "list_v1",
        channel_id: 404,
        conversation_type: "direct",
        name: "Cliente compacto",
        state: "open",
        ignored: false,
        unread_count: 2,
        first_unread_message_id: 41,
        preference: {pinned: true, muted: false},
        account: {id: 1, name: "Comercial", platform: "whatsapp"},
        capabilities: {delete_conversation: true, ignore_conversation: true},
        identity: {id: 31, name: "Cliente compacto", avatar_url: false},
        group: false,
        responsible: {id: 9, name: "Agente"},
        tags: [],
        platform: "whatsapp",
    };
}

function fullDetail() {
    const value = compactRow();
    delete value.projection;
    return {
        ...value,
        can_send: true,
        capabilities: {...value.capabilities, reply: true},
    };
}

class EmptyComponent extends Component {}
EmptyComponent.template = xml`<span/>`;
EmptyComponent.props = ["*"];

class TimelineMarker extends Component {}
TimelineMarker.template = xml`<div class="cc-test-detail-timeline"/>`;
TimelineMarker.props = ["*"];
class ComposerMarker extends Component {}
ComposerMarker.template = xml`<div class="cc-test-detail-composer"/>`;
ComposerMarker.props = ["*"];

// Exercise the production app template and its readiness branches while leaving
// the unrelated list, timeline and composer lifecycle to their own suites.
class DetailApp extends ContactCenterApp {
    setup() {
        this.store = this.props.store;
        this.store.state = useState(this.store.state);
        this.ui = useState({sidePanel: "contact", stateChanging: false});
    }
}
DetailApp.props = {store: Object};
DetailApp.components = {
    ...ContactCenterApp.components,
    ConversationList: EmptyComponent,
    ContactPanel: EmptyComponent,
    ConversationTimeline: TimelineMarker,
    MessageComposer: ComposerMarker,
    ConversationTags: EmptyComponent,
};

function pendingStore() {
    const store = new ContactCenterStore({
        orm: {},
        busService: new EventTarget(),
        stateFactory: reactive,
        notification: false,
    });
    store.state.phase = "ready";
    store.state.bootstrap = {user: {id: 9}, capabilities: {internal_notes: true}};
    store.state.conversations = [compactRow()];
    store.state.selectedChannelId = 404;
    Object.assign(store.state.selectedDetail, {channelId: 404, status: "loading"});
    return store;
}

QUnit.module("contact_center_ui > selected detail components", (hooks) => {
    hooks.beforeEach(() => {
        makeFakeLocalizationService();
        const banner = document.getElementById("oe_neutralize_banner");
        if (banner) {
            (banner.parentElement || banner).remove();
        }
    });

    QUnit.test("compact row renders the complete conversation menu", async (assert) => {
        registry.category("services").add("hotkey", hotkeyService);
        registry.category("services").add("ui", uiService);
        const target = getFixture();
        const actions = [];
        await mount(ConversationMenu, target, {
            env: await makeTestEnv(),
            props: {
                conversation: compactRow(),
                busy: false,
                onAction: (channelId, action) => actions.push([channelId, action]),
            },
        });
        await click(target, ".cc-conversation-item__menu-toggle");
        const menu = target.querySelector(".cc-conversation-menu");
        assert.ok(menu, "the actual dropdown template opens");
        for (const label of [
            "Arquivar conversa",
            "Silenciar conversa",
            "Desafixar conversa",
            "Marcar como lida",
            "Ignorar contato",
            "Excluir conversa",
        ]) {
            assert.ok(menu.textContent.includes(label), label);
        }
        const ignore = [...menu.querySelectorAll(".dropdown-item")].find((item) =>
            item.textContent.includes("Ignorar contato")
        );
        await click(ignore);
        assert.deepEqual(actions, [[404, "ignored"]]);
    });

    QUnit.test(
        "compact data cannot authorize app, chat, composer or timeline actions",
        (assert) => {
            const conversation = compactRow();
            const store = {
                selectedConversation: conversation,
                permissionReady: false,
                capabilities: {internal_notes: true, view_technical_message: true},
                state: {selectedDetail: {channelId: 404, status: "loading"}},
            };
            const app = Object.create(ContactCenterApp.prototype);
            app.store = store;
            const chat = Object.create(ContactCenterChat.prototype);
            chat.store = store;
            const composer = Object.create(MessageComposer.prototype);
            composer.props = {store, state: store.state, conversation};
            const timeline = Object.create(ConversationTimeline.prototype);
            timeline.props = {store, state: {messages: []}};
            assert.notOk(app.canShowComposer);
            assert.notOk(
                chat.canCompose,
                "global internal-note capability cannot bypass first detail"
            );
            assert.notOk(composer.conversationPolicy.allow_send);
            assert.notOk(composer.canUseInternalNotes);
            assert.deepEqual(composer.capabilities, {});
            assert.notOk(
                timeline.messageActionAllowed({actions: {reply: true}}, "reply")
            );
            store.selectedConversation = fullDetail();
            composer.props.conversation = store.selectedConversation;
            store.permissionReady = true;
            assert.ok(app.canShowComposer);
            assert.ok(chat.canCompose);
            assert.ok(composer.conversationPolicy.allow_send);
            assert.ok(composer.canUseInternalNotes);
            assert.ok(timeline.messageActionAllowed({actions: {reply: true}}, "reply"));
            store.state.selectedDetail.status = "loading";
            assert.ok(
                chat.canCompose,
                "revalidation retains the authorized snapshot under R3"
            );
            store.permissionReady = false;
            assert.notOk(chat.canCompose, "effective denial revokes composition");
        }
    );

    QUnit.test(
        "initial loading mounts sensitive conversation components only after authorized ready",
        async (assert) => {
            const store = pendingStore();
            const target = getFixture();
            try {
                await mount(DetailApp, target, {
                    env: {services: {}},
                    props: {store},
                });
                assert.ok(
                    target.querySelector(".cc-selected-detail-status[aria-busy='true']")
                );
                assert.notOk(target.querySelector(".cc-test-detail-timeline"));
                assert.notOk(target.querySelector(".cc-test-detail-composer"));
                assert.notOk(target.querySelector(".cc-read-only-composer"));
                store.replaceConversation(fullDetail(), {insert: true});
                await nextTick();
                const timeline = target.querySelector(".cc-test-detail-timeline");
                const composer = target.querySelector(".cc-test-detail-composer");
                assert.ok(timeline);
                assert.ok(composer);
                assert.notOk(target.querySelector(".cc-selected-detail-status"));
                store.state.selectedDetail.status = "loading";
                await nextTick();
                assert.strictEqual(
                    target.querySelector(".cc-test-detail-timeline"),
                    timeline
                );
                assert.strictEqual(
                    target.querySelector(".cc-test-detail-composer"),
                    composer,
                    "revalidation does not unmount an authorized composer"
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "first detail error renders retry and never renders a blocked-conversation footer",
        async (assert) => {
            const store = pendingStore();
            const target = getFixture();
            let retries = 0;
            store.retrySelectedDetail = async () => {
                retries += 1;
                store.replaceConversation(fullDetail(), {insert: true});
                return true;
            };
            try {
                await mount(DetailApp, target, {
                    env: {services: {}},
                    props: {store},
                });
                assert.ok(
                    target.querySelector(".cc-selected-detail-status[role='status']")
                );
                assert.notOk(target.querySelector(".cc-read-only-composer"));
                Object.assign(store.state.selectedDetail, {
                    status: "error",
                    error: "Falha temporária ao carregar detalhes.",
                });
                await nextTick();
                assert.ok(
                    target.querySelector(".cc-selected-detail-status[role='alert']")
                );
                assert.ok(target.textContent.includes("Falha temporária"));
                await click(target, ".cc-selected-detail-status button");
                await nextTick();
                assert.strictEqual(retries, 1);
                assert.ok(store.selectedDetailReady);
                assert.notOk(target.querySelector(".cc-selected-detail-status"));
                assert.ok(target.querySelector(".cc-test-detail-timeline"));
                assert.ok(target.querySelector(".cc-test-detail-composer"));
            } finally {
                store.destroy();
            }
        }
    );
});
