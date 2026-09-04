/** @odoo-module **/

import {Component, onWillDestroy, useRef, useState} from "@odoo/owl";
import {
    conversationAvatarUrl,
    conversationDisplayName,
    filterConversationsByResponsibility,
    groupMetadataUi,
    initials,
    isGroupConversation,
    isResponsibilityScope,
    messagePreviewText,
    messageStatusMeta,
    realtimeStatusMeta,
} from "./contact_center_model.esm";
import {ConnectionHealth} from "./connection_health.esm";
import {DeferredImage} from "./deferred_image.esm";
import {deserializeDateTime} from "@web/core/l10n/dates";

const {DateTime} = luxon;
const LIST_VIEWS = new Set(["grouped", "flat"]);

function positiveInteger(value) {
    return Number.isSafeInteger(value) && value > 0 ? value : false;
}

function isPlainRecord(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        return false;
    }
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
}

function ownValue(record, name) {
    return Object.prototype.hasOwnProperty.call(record, name)
        ? record[name]
        : undefined;
}

function renderableConversations(conversations) {
    return filterConversationsByResponsibility(conversations, "all", false);
}

function safeInboxName(value, fallback) {
    return typeof value === "string" && value.trim()
        ? value.trim().slice(0, 160)
        : fallback;
}

function safePlatform(value) {
    return typeof value === "string" ? value.trim().slice(0, 40) : "";
}

function editableShortcutTarget(target) {
    if (!target || typeof target !== "object") {
        return false;
    }
    const tagName = String(target.tagName || "").toLowerCase();
    return (
        ["input", "textarea", "select"].includes(tagName) ||
        target.isContentEditable === true ||
        (typeof target.closest === "function" &&
            Boolean(target.closest("[contenteditable='true']")))
    );
}

export function conversationListShortcut(event) {
    if (
        !event ||
        event.isComposing ||
        editableShortcutTarget(event.target) ||
        event.ctrlKey ||
        event.metaKey
    ) {
        return false;
    }
    if (event.key === "/" && !event.altKey && !event.shiftKey) {
        return "search";
    }
    if (event.altKey && !event.shiftKey && event.key === "ArrowUp") {
        return "previous";
    }
    if (event.altKey && !event.shiftKey && event.key === "ArrowDown") {
        return "next";
    }
    return false;
}

export function conversationInboxMetadata(conversation) {
    const account =
        conversation &&
        typeof conversation === "object" &&
        Object.prototype.hasOwnProperty.call(conversation, "account")
            ? conversation.account
            : false;
    if (!isPlainRecord(account)) {
        return {
            key: "inbox:unknown",
            id: false,
            name: "Caixa não identificada",
            platform: "",
        };
    }
    const accountId = positiveInteger(ownValue(account, "id"));
    if (!accountId) {
        return {
            key: "inbox:unknown",
            id: false,
            name: "Caixa não identificada",
            platform: "",
        };
    }
    return {
        key: `inbox:${accountId}`,
        id: accountId,
        name: safeInboxName(ownValue(account, "name"), "Caixa sem nome"),
        platform: safePlatform(ownValue(account, "platform")),
    };
}

export function groupConversationsByInbox(conversations, accounts = []) {
    const groups = new Map();
    for (const conversation of renderableConversations(conversations)) {
        const inbox = conversationInboxMetadata(conversation);
        if (!groups.has(inbox.key)) {
            groups.set(inbox.key, {
                ...inbox,
                conversations: [],
                unread_count: 0,
            });
        }
        const group = groups.get(inbox.key);
        group.conversations.push(conversation);
        if (
            conversation &&
            Number.isSafeInteger(conversation.unread_count) &&
            conversation.unread_count > 0
        ) {
            group.unread_count += conversation.unread_count;
        }
    }
    for (const account of Array.isArray(accounts) ? accounts : []) {
        const inbox = conversationInboxMetadata({account});
        if (!inbox.id || groups.has(inbox.key)) {
            continue;
        }
        groups.set(inbox.key, {
            ...inbox,
            conversations: [],
            unread_count: 0,
        });
    }
    return [...groups.values()];
}

export function conversationPreviewText(conversation) {
    if (!conversation || !conversation.last_message) {
        return "Conversa iniciada";
    }
    const preview = messagePreviewText(conversation.last_message);
    const author = conversation.last_message.author;
    if (
        isGroupConversation(conversation) &&
        conversation.last_message.direction === "inbound" &&
        author &&
        typeof author.name === "string" &&
        author.name.trim()
    ) {
        return `${author.name.trim()}: ${preview}`;
    }
    return preview;
}

export function conversationGroupBadgeLabel(conversation) {
    const group = groupMetadataUi(conversation);
    if (!group) {
        return "";
    }
    if (group.participant_count === false) {
        return "Grupo";
    }
    return `Grupo · ${group.participant_count}`;
}

export class ConversationList extends Component {
    setup() {
        this.searchRef = useRef("search");
        this.ui = useState({
            view: "grouped",
            collapsedInboxes: {},
            compactToolsOpen: false,
        });
        this.onWindowKeydown = (event) => this.onShortcut(event);
        window.addEventListener("keydown", this.onWindowKeydown);
        onWillDestroy(() => {
            window.removeEventListener("keydown", this.onWindowKeydown);
        });
    }

    get state() {
        return this.props.state;
    }

    get store() {
        return this.props.store;
    }

    get attentionMeta() {
        const attention = this.state.attention || {};
        if (attention.permission === "granted") {
            return {
                state: "active",
                icon: "fa fa-bell",
                label: "Alertas externos ativos",
            };
        }
        if (attention.permission === "denied") {
            return {
                state: attention.sound_enabled ? "sound" : "blocked",
                icon: attention.sound_enabled ? "fa fa-volume-up" : "fa fa-bell-slash",
                label: attention.sound_enabled
                    ? "Som ativo; notificações bloqueadas pelo navegador"
                    : "Notificações bloqueadas pelo navegador",
            };
        }
        return {
            state: attention.sound_enabled ? "sound" : "idle",
            icon: attention.sound_enabled ? "fa fa-volume-up" : "fa fa-bell-o",
            label: attention.sound_enabled
                ? "Som de novas mensagens ativo"
                : "Ativar som e notificações fora desta aba",
        };
    }

    enableAttention() {
        return this.store.enableAttention();
    }

    get filteredConversations() {
        return this.store.responsibilityVisibleConversations;
    }

    get conversationGroups() {
        const selectedAccountId = positiveInteger(this.state.filters.accountId);
        const accounts = selectedAccountId
            ? this.store.accounts.filter((account) => account.id === selectedAccountId)
            : this.store.accounts;
        return groupConversationsByInbox(this.filteredConversations, accounts);
    }

    get displayedCount() {
        return String(this.store.displayedConversationTotal);
    }

    get realtimeMeta() {
        return realtimeStatusMeta(this.state.realtime);
    }

    get compactMode() {
        return this.state.inboxDensity === "compact";
    }

    get densityButtonLabel() {
        return this.compactMode ? "Expandir lista" : "Usar lista compacta";
    }

    initials(name) {
        return initials(name);
    }

    displayName(conversation) {
        return conversationDisplayName(conversation);
    }

    avatarUrl(conversation) {
        return conversationAvatarUrl(conversation);
    }

    groupBadgeLabel(conversation) {
        return conversationGroupBadgeLabel(conversation);
    }

    inboxMetadata(conversation) {
        return conversationInboxMetadata(conversation);
    }

    itemClass(conversation) {
        const classes = ["cc-conversation-item"];
        if (this.ui.view === "grouped") {
            classes.push("cc-conversation-item--grouped");
        }
        if (conversation.channel_id === this.state.selectedChannelId) {
            classes.push("is-active");
        }
        if (conversation.unread_count) {
            classes.push("has-unread");
        }
        return classes.join(" ");
    }

    relativeDate(value) {
        if (!value) {
            return "";
        }
        const date = deserializeDateTime(value);
        const now = DateTime.local();
        if (date.hasSame(now, "day")) {
            return date.toFormat("HH:mm");
        }
        if (date.hasSame(now.minus({days: 1}), "day")) {
            return "Ontem";
        }
        if (date.year === now.year) {
            return date.toFormat("dd/LL");
        }
        return date.toFormat("dd/LL/yy");
    }

    deliveryIcon(conversation) {
        const message = conversation.last_message;
        return message && message.direction === "outbound"
            ? messageStatusMeta(message.delivery_state, message.dispatch_state).icon
            : "";
    }

    deliveryClass(conversation) {
        const message = conversation.last_message;
        const state = message
            ? messageStatusMeta(message.delivery_state, message.dispatch_state).state
            : "";
        return `cc-delivery-inline cc-delivery-inline--${state || "queued"}`;
    }

    deliveryLabel(conversation) {
        const message = conversation.last_message;
        return message
            ? messageStatusMeta(message.delivery_state, message.dispatch_state).label
            : "";
    }

    preview(conversation) {
        return conversationPreviewText(conversation);
    }

    isGroup(conversation) {
        return isGroupConversation(conversation);
    }

    onSearchInput(event) {
        this.store.setFilter("query", event.target.value);
    }

    onStateClick(state) {
        this.store.setFilter("state", state);
    }

    onAccountChange(event) {
        const value = event.target.value;
        this.store.setFilter("accountId", value ? Number(value) : false);
    }

    setResponsibility(scope) {
        if (isResponsibilityScope(scope)) {
            this.store.setFilter("responsibility", scope);
        }
    }

    toggleUnreadOnly() {
        this.store.setFilter("unreadOnly", !this.state.filters.unreadOnly);
    }

    setConversationType(type) {
        this.store.setFilter("conversationType", type || false);
    }

    onTagChange(event) {
        this.store.setFilter("tagId", event.target.value || false);
    }

    onActivityTimingChange(event) {
        this.store.setFilter("activityTiming", event.target.value || false);
    }

    navigateConversation(offset) {
        const conversations = this.filteredConversations;
        if (!conversations.length) {
            return false;
        }
        const currentIndex = conversations.findIndex(
            (item) => item.channel_id === this.state.selectedChannelId
        );
        const origin = currentIndex >= 0 ? currentIndex : offset > 0 ? -1 : 0;
        const nextIndex =
            (origin + offset + conversations.length) % conversations.length;
        this.select(conversations[nextIndex]);
        return true;
    }

    onShortcut(event) {
        const shortcut = conversationListShortcut(event);
        if (!shortcut) {
            return false;
        }
        event.preventDefault();
        if (shortcut === "search") {
            if (this.compactMode) {
                this.ui.compactToolsOpen = true;
            }
            if (this.searchRef.el) {
                this.searchRef.el.focus();
            }
            return true;
        }
        return this.navigateConversation(shortcut === "next" ? 1 : -1);
    }

    setView(view) {
        if (LIST_VIEWS.has(view)) {
            this.ui.view = view;
        }
    }

    toggleDensity() {
        this.ui.compactToolsOpen = false;
        this.store.toggleInboxDensity();
    }

    toggleCompactTools() {
        this.ui.compactToolsOpen = !this.ui.compactToolsOpen;
    }

    inboxCollapsed(key) {
        return this.ui.collapsedInboxes[key] === true;
    }

    toggleInbox(key) {
        if (typeof key === "string" && key.startsWith("inbox:")) {
            this.ui.collapsedInboxes[key] = !this.inboxCollapsed(key);
        }
    }

    inboxCountLabel(group) {
        const count = group.conversations.length;
        const label = `${count} ${count === 1 ? "conversa" : "conversas"}`;
        return this.state.conversationsHaveMore
            ? `${label} ${count === 1 ? "carregada" : "carregadas"}`
            : label;
    }

    inboxUnreadLabel(group) {
        const label = `${group.unread_count} ${
            group.unread_count === 1 ? "mensagem não lida" : "mensagens não lidas"
        }`;
        return this.state.conversationsHaveMore
            ? `${label} nas conversas carregadas`
            : label;
    }

    inboxPanelId(group) {
        return `cc-inbox-group-${group.id || "unknown"}`;
    }

    select(conversation) {
        this.store.selectConversation(conversation.channel_id);
    }
}

ConversationList.props = {state: Object, store: Object};
ConversationList.components = {ConnectionHealth, DeferredImage};
ConversationList.template = "contact_center_ui.ConversationList";
