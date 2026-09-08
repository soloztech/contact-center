/** @odoo-module **/

import {Component, onWillDestroy, useRef, useState} from "@odoo/owl";
import {
    conversationAvatarUrl,
    conversationDisplayName,
    conversationPreference,
    filterConversationsByResponsibility,
    groupMetadataUi,
    initials,
    isGroupConversation,
    isResponsibilityScope,
    messagePreviewText,
    messageStatusMeta,
    realtimeStatusMeta,
} from "./contact_center_model.esm";
import {ConfirmationDialog} from "@web/core/confirmation_dialog/confirmation_dialog";
import {ConnectionHealth} from "./connection_health.esm";
import {DeferredImage} from "./deferred_image.esm";
import {Dropdown} from "@web/core/dropdown/dropdown";
import {DropdownItem} from "@web/core/dropdown/dropdown_item";
import {deserializeDateTime} from "@web/core/l10n/dates";
import {useOwnedDialogs} from "@web/core/utils/hooks";

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

export class ConversationMenu extends Component {
    get label() {
        return `Opções da conversa ${conversationDisplayName(this.props.conversation)}`;
    }

    get preference() {
        return conversationPreference(this.props.conversation);
    }

    can(action) {
        const capabilities = this.props.conversation.capabilities || {};
        return capabilities[action] === true;
    }

    get ignoreLabel() {
        if (this.props.conversation.ignored === true) {
            return "Deixar de ignorar";
        }
        return isGroupConversation(this.props.conversation)
            ? "Ignorar grupo"
            : "Ignorar contato";
    }

    selectAction(action) {
        return this.props.onAction(this.props.conversation.channel_id, action);
    }
}

ConversationMenu.props = {conversation: Object, busy: Boolean, onAction: Function};
ConversationMenu.components = {Dropdown, DropdownItem};
ConversationMenu.template = "contact_center_ui.ConversationMenu";

export class ConversationList extends Component {
    setup() {
        this.searchRef = useRef("search");
        this.addDialog = useOwnedDialogs();
        this.ui = useState({
            pendingConversationIds: {},
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
        const user = this.state.bootstrap && this.state.bootstrap.user;
        return filterConversationsByResponsibility(
            this.state.conversations,
            this.state.filters.responsibility,
            positiveInteger(user && user.id)
        );
    }

    get accounts() {
        return (this.state.bootstrap && this.state.bootstrap.accounts) || [];
    }

    get conversationGroups() {
        const selectedAccountId = positiveInteger(this.state.filters.accountId);
        const accounts = selectedAccountId
            ? this.accounts.filter((account) => account.id === selectedAccountId)
            : this.accounts;
        return groupConversationsByInbox(this.filteredConversations, accounts);
    }

    get displayedCount() {
        const total =
            Number.isSafeInteger(this.state.conversationTotal) &&
            this.state.conversationTotal >= 0
                ? this.state.conversationTotal
                : 0;
        return String(Math.max(total, this.state.conversations.length));
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
        const preference = conversationPreference(conversation);
        if (preference.pinned) {
            classes.push("is-pinned");
        }
        if (preference.muted) {
            classes.push("is-muted");
        }
        return classes.join(" ");
    }

    preference(conversation) {
        return conversationPreference(conversation);
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

    async runConversationAction(channelId, action) {
        const conversation = this.state.conversations.find(
            (item) => item.channel_id === channelId
        );
        if (
            !conversation ||
            this.ui.pendingConversationIds[channelId] ||
            !["pinned", "muted", "archived", "unread", "ignored", "delete"].includes(
                action
            )
        ) {
            return false;
        }
        if (action === "delete" || action === "ignored") {
            return this.confirmConversationAction(conversation, action);
        }
        this.ui.pendingConversationIds[channelId] = true;
        try {
            if (action === "pinned") {
                return await this.store.toggleConversationPinned(channelId);
            }
            if (action === "muted") {
                return await this.store.toggleConversationMuted(channelId);
            }
            if (action === "unread") {
                return await this.store.markConversationUnread(channelId);
            }
            return await this.store.setConversationState(
                conversation.state === "archived" ? "open" : "archived",
                channelId
            );
        } finally {
            delete this.ui.pendingConversationIds[channelId];
        }
    }

    confirmConversationAction(conversation, action) {
        const capability =
            action === "delete" ? "delete_conversation" : "ignore_conversation";
        if (
            !conversation.capabilities ||
            conversation.capabilities[capability] !== true
        ) {
            return false;
        }
        const channelId = conversation.channel_id;
        const ignored = conversation.ignored !== true;
        const target = isGroupConversation(conversation) ? "grupo" : "contato";
        let title = "Excluir conversa?";
        let body =
            "A conversa e todas as suas mensagens serão excluídas da Central de Atendimento. " +
            "Esta ação não pode ser desfeita. Mensagens já enviadas no canal externo permanecerão lá.";
        let confirmLabel = "Excluir conversa";
        if (action === "ignored") {
            title = ignored
                ? `Ignorar este ${target}?`
                : `Deixar de ignorar este ${target}?`;
            body = ignored
                ? `Novas mensagens deste ${target} não serão registradas nesta caixa, para nenhum atendente. ` +
                  "O histórico existente será mantido. Você poderá deixar de ignorar pelo mesmo menu."
                : `As próximas mensagens deste ${target} voltarão a ser registradas nesta caixa. ` +
                  "As mensagens recebidas enquanto estava ignorado não serão recuperadas.";
            confirmLabel = ignored ? "Ignorar" : "Deixar de ignorar";
        }
        this.ui.pendingConversationIds[channelId] = true;
        const release = () => delete this.ui.pendingConversationIds[channelId];
        this.addDialog(
            ConfirmationDialog,
            {
                title,
                body: `${conversationDisplayName(conversation)} — ${body}`,
                confirmLabel,
                cancelLabel: "Cancelar",
                confirm: async () => {
                    try {
                        if (action === "delete") {
                            return await this.store.deleteConversation(channelId);
                        }
                        return await this.store.setConversationIgnored(
                            ignored,
                            channelId
                        );
                    } finally {
                        release();
                    }
                },
                cancel: release,
            },
            {onClose: release}
        );
        return true;
    }
}

ConversationList.props = {state: Object, store: Object};
ConversationList.components = {ConnectionHealth, DeferredImage, ConversationMenu};
ConversationList.template = "contact_center_ui.ConversationList";
