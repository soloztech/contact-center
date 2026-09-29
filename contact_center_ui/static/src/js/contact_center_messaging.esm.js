/** @odoo-module **/

import "@mail/components/chat_window/chat_window";
import {Component, onWillDestroy, onWillStart, useEffect, useState} from "@odoo/owl";
import {
    contactCenterNotifications,
    conversationAvatarUrl,
    conversationDisplayName,
    conversationUiPolicy,
    initials,
    isRenderableConversation,
    messagePreviewText,
    validateEnvelope,
} from "./contact_center_model.esm";
import {ContactCenterStore} from "./contact_center_store.esm";
import {ConversationTimeline} from "./conversation_timeline.esm";
import {DeferredImage} from "./deferred_image.esm";
import {MessageComposer} from "./message_composer.esm";
import {MessagingMenu} from "@mail/components/messaging_menu/messaging_menu";
import {SystraySummary} from "./contact_center_systray.esm";
import {attr} from "@mail/model/model_field";
import {browser} from "@web/core/browser/browser";
import {getMessagingComponent} from "@mail/utils/messaging_component";
import {patch} from "@web/core/utils/patch";
import {registerPatch} from "@mail/model/model_core";
import {useModels} from "@mail/component_hooks/use_models";
import {useService} from "@web/core/utils/hooks";

const TAB = "contact_center";

// The existing native window manager owns placement, folding, overflow and
// mobile fullscreen. No mail Thread is created: all reads and sends continue
// through the Contact Center API and its authorization/provider policies.
export function openContactCenterChat(messaging, conversation) {
    const chatWindow = messaging.models.ChatWindow.insert({
        contactCenterChannelId: conversation.channel_id,
        contactCenterName: conversationDisplayName(conversation),
        manager: messaging.chatWindowManager,
    });
    chatWindow.makeActive({notifyServer: false});
    messaging.messagingMenu.close();
    return chatWindow;
}

registerPatch({
    name: "ChatWindow",
    fields: {
        contactCenterChannelId: attr({identifying: true}),
        contactCenterName: attr(),
        contactCenterComponent: attr(),
        contactCenterDoFocus: attr({default: false}),
        hasNewMessageForm: {
            compute() {
                return this.contactCenterChannelId ? false : this._super();
            },
        },
        name: {
            compute() {
                return this.contactCenterChannelId
                    ? this.contactCenterName || "Contact Center"
                    : this._super();
            },
        },
    },
    recordMethods: {
        close(options) {
            if (
                this.contactCenterComponent &&
                !this.contactCenterComponent.canClose()
            ) {
                return;
            }
            return this._super(options);
        },
        focus() {
            if (this.contactCenterChannelId) {
                this.update({contactCenterDoFocus: true});
                return;
            }
            return this._super();
        },
        onKeydown(event) {
            // Keep Tab inside the operational composer's controls, including
            // attachment and recording dialogs. Native Escape still closes.
            if (
                this.contactCenterChannelId &&
                (event.key === "Tab" || event.defaultPrevented)
            ) {
                return;
            }
            return this._super(event);
        },
    },
});

registerPatch({
    name: "ChatWindowManager",
    fields: {
        visual: {
            compute() {
                const native = this._super();
                if (
                    !this.messaging ||
                    !this.messaging.device ||
                    this.messaging.device.isSmall ||
                    !this.messaging.discuss.discussView ||
                    this.messaging.discussPublicView
                ) {
                    return native;
                }
                // Discuss normally hides every chat window. Contact Center has
                // no native Discuss thread, so its shortcut must stay visible
                // even when the underlying action happens to be Discuss.
                const windows = this.chatWindows.filter(
                    (win) => win.contactCenterChannelId
                );
                if (!windows.length) {
                    return native;
                }
                const width =
                    this.messaging.device.globalWindowInnerWidth -
                    this.startGapWidth -
                    this.endGapWidth;
                const step = this.chatWindowWidth + this.betweenGapWidth;
                const slots = Math.max(0, Math.floor(width / step));
                const count =
                    windows.length <= slots
                        ? slots
                        : Math.max(
                              0,
                              Math.floor(
                                  (width -
                                      this.hiddenMenuWidth -
                                      this.betweenGapWidth) /
                                      step
                              )
                          );
                const visible = windows.slice(0, count).map((chatWindow, index) => ({
                    chatWindow,
                    offset: this.startGapWidth + index * step,
                }));
                const hidden = windows.slice(count);
                return {
                    availableVisibleSlots: count,
                    visible,
                    hiddenChatWindows: hidden,
                    isHiddenMenuVisible: Boolean(hidden.length),
                    hiddenMenuOffset: this.startGapWidth + visible.length * step,
                };
            },
        },
    },
});

export class ContactCenterMessagingList extends Component {
    setup() {
        this.orm = useService("orm");
        this.messagingService = useService("messaging");
        const bus = useService("bus_service");
        this.state = useState({
            items: [],
            phase: "loading",
            cursor: false,
            more: false,
        });
        this.request = 0;
        this.destroyed = false;
        this.timer = null;
        const schedule = () => {
            if (this.timer === null) {
                this.timer = browser.setTimeout(() => {
                    this.timer = null;
                    this.load();
                }, 800);
            }
        };
        const onNotification = (event) => {
            if (contactCenterNotifications(event.detail).length) {
                schedule();
            }
        };
        onWillStart(() => {
            bus.addEventListener("notification", onNotification);
            bus.addEventListener("reconnect", schedule);
            browser.addEventListener("focus", schedule);
            this.load();
        });
        onWillDestroy(() => {
            this.destroyed = true;
            this.request += 1;
            browser.clearTimeout(this.timer);
            bus.removeEventListener("notification", onNotification);
            bus.removeEventListener("reconnect", schedule);
            browser.removeEventListener("focus", schedule);
        });
    }

    async load(more = false) {
        if (more && (!this.state.more || this.state.phase === "loading")) {
            return;
        }
        const request = ++this.request;
        this.state.phase = "loading";
        try {
            const payload = await this.orm.silent.call(
                "contact.center.ui.api",
                "list_conversations",
                [],
                {
                    limit: 30,
                    cursor: more ? this.state.cursor : false,
                    filters: {responsibility: "mine"},
                }
            );
            if (this.destroyed || request !== this.request) {
                return;
            }
            validateEnvelope(payload);
            if (!Array.isArray(payload.items)) {
                throw new Error("Invalid conversation list");
            }
            const items = more ? this.state.items : [];
            const merged = new Map(items.map((item) => [item.channel_id, item]));
            for (const item of payload.items.filter(isRenderableConversation)) {
                merged.set(item.channel_id, item);
            }
            this.state.items = [...merged.values()];
            this.state.more = Boolean(payload.has_more && payload.next_cursor);
            this.state.cursor = payload.next_cursor || false;
            this.state.phase = "ready";
        } catch (_error) {
            if (!this.destroyed && request === this.request) {
                // A failed authorization/refresh must not keep stale previews.
                this.state.items = [];
                this.state.phase = "error";
            }
        }
    }

    async open(conversation) {
        const messaging = await this.messagingService.get();
        if (!this.destroyed) {
            openContactCenterChat(messaging, conversation);
        }
    }

    name(conversation) {
        return conversationDisplayName(conversation);
    }

    avatar(conversation) {
        return conversationAvatarUrl(conversation);
    }

    initials(conversation) {
        return initials(this.name(conversation));
    }

    preview(conversation) {
        return conversation.last_message
            ? messagePreviewText(conversation.last_message)
            : "Conversa iniciada";
    }
}
ContactCenterMessagingList.template = "contact_center_ui.MessagingList";
ContactCenterMessagingList.props = {};
ContactCenterMessagingList.components = {DeferredImage};

patch(
    getMessagingComponent("MessagingMenu").components,
    "contact_center_ui.messaging_components",
    {
        ContactCenterMessagingList,
    }
);
patch(MessagingMenu.prototype, "contact_center_ui.messaging_menu", {
    setup() {
        this._super(...arguments);
        this.contactCenter = useState({enabled: false, mine: 0, all: 0});
        const orm = useService("orm");
        const busService = useService("bus_service");
        const user = useService("user");
        const summary = new SystraySummary({
            call: () => orm.silent.call("contact.center.ui.api", "systray_summary", []),
            userId: user.userId,
            onChange: (value) => Object.assign(this.contactCenter, value),
        });
        const onNotification = (event) => summary.handleNotifications(event.detail);
        const onFocus = () => summary.schedule();
        onWillStart(() => {
            summary.start();
            busService.addEventListener("notification", onNotification);
            browser.addEventListener("focus", onFocus);
        });
        onWillDestroy(() => {
            summary.destroy();
            busService.removeEventListener("notification", onNotification);
            browser.removeEventListener("focus", onFocus);
        });
    },
    get contactCenterCounter() {
        return this.messagingMenu.counter + this.contactCenter.mine;
    },
    openContactCenterTab() {
        this.messagingMenu.update({activeTabId: TAB});
    },
});

export class ContactCenterChat extends Component {
    setup() {
        useModels();
        this.ui = useState({loading: true});
        this.store = new ContactCenterStore({
            orm: useService("orm"),
            busService: useService("bus_service"),
            notification: useService("notification"),
            stateFactory: useState,
            initialActionParams: {
                channel_id: this.props.chatWindow.contactCenterChannelId,
            },
            // The shortcut never takes ownership of the full inbox's filters,
            // scroll position or selected conversation.
            inboxPreferenceStorage: false,
        });
        this.store.state.detailsOpen = false;
        this.props.chatWindow.update({contactCenterComponent: this});
        onWillStart(() => {
            this.store.start().then(() => {
                if (!this.store.destroyed) {
                    this.ui.loading = false;
                }
            });
        });
        useEffect(
            () => {
                const chatWindow = this.props.chatWindow;
                if (
                    chatWindow.contactCenterDoFocus &&
                    !chatWindow.isFolded &&
                    chatWindow.isVisible &&
                    this.focus()
                ) {
                    chatWindow.update({contactCenterDoFocus: false});
                }
            },
            () => [
                this.ui.loading,
                this.conversation,
                this.props.chatWindow.contactCenterDoFocus,
                this.props.chatWindow.isFolded,
                this.props.chatWindow.isVisible,
            ]
        );
        onWillDestroy(() => {
            const chatWindow = this.props.chatWindow;
            if (chatWindow.exists() && chatWindow.contactCenterComponent === this) {
                chatWindow.update({contactCenterComponent: undefined});
            }
            this.store.destroy();
        });
    }

    get conversation() {
        return this.store.selectedConversation;
    }

    get canCompose() {
        return Boolean(
            this.conversation &&
                (conversationUiPolicy(this.conversation).show_composer ||
                    this.store.capabilities.internal_notes)
        );
    }

    canClose() {
        const guard = this.store.conversationSelectionGuard;
        if (
            this.store.isSending(this.store.state.selectedChannelId) ||
            (guard && !guard(false, {checkOnly: true}))
        ) {
            this.store.notify(
                "Conclua ou cancele a ação em andamento antes de fechar.",
                {
                    type: "warning",
                }
            );
            return false;
        }
        return true;
    }

    focus() {
        const component = this.props.chatWindow;
        const selector = `.o_ChatWindow[data-contact-center-id="${component.contactCenterChannelId}"] .cc-composer textarea`;
        const input = document.querySelector(selector);
        if (input) {
            input.focus();
            return true;
        }
        return false;
    }

    retry() {
        this.store.initialNavigation = {
            channelId: this.props.chatWindow.contactCenterChannelId,
        };
        return this.store.loadBootstrap();
    }
}
ContactCenterChat.template = "contact_center_ui.Chat";
ContactCenterChat.props = {chatWindow: Object};
ContactCenterChat.components = {ConversationTimeline, MessageComposer};
patch(
    getMessagingComponent("ChatWindow").components,
    "contact_center_ui.chat_components",
    {
        ContactCenterChat,
    }
);
