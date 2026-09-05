/** @odoo-module **/

import {Component, useEffect, useRef, useState} from "@odoo/owl";
import {MessageContent, controlTimelineMessageMeta} from "./message_content.esm";
import {
    conversationUiPolicy,
    initials,
    messageActionEnabled,
    messageDispatchReasonMeta,
    messagePreviewText,
    messageStatusMeta,
    sourceWebhookActionEnabled,
} from "./contact_center_model.esm";
import {deserializeDateTime, formatDate} from "@web/core/l10n/dates";
import {_t} from "@web/core/l10n/translation";
import {browser} from "@web/core/browser/browser";
import {useService} from "@web/core/utils/hooks";

const {DateTime} = luxon;
const QUICK_REACTIONS = Object.freeze(["👍", "❤️", "😂", "😮", "😢", "🙏"]);
const TIMELINE_BOTTOM_THRESHOLD = 80;
const MESSAGE_RUN_WINDOW_MINUTES = 5;

/**
 * Control events are operational timeline records, not human chat messages.
 * Keep this predicate fail-closed so an unknown provider content type cannot
 * accidentally acquire the privileged, author-less control presentation.
 *
 * @param {Object} message timeline message DTO
 * @returns {Boolean} whether the item uses the control-event presentation
 */
export function isControlTimelineMessage(message) {
    return Boolean(controlTimelineMessageMeta(message));
}

export function isForwardedTimelineMessage(message) {
    return Boolean(
        message && message.is_forwarded === true && message.is_deleted !== true
    );
}

function messageRunAuthorKey(message) {
    const author = message && message.author;
    if (
        !author ||
        !["guest", "partner"].includes(author.type) ||
        !Number.isSafeInteger(author.id) ||
        author.id <= 0
    ) {
        return false;
    }
    return `${author.type}:${author.id}`;
}

function messageHasRenderableContent(message) {
    if (!message || message.is_deleted) {
        return false;
    }
    const hasBody =
        typeof message.body_text === "string" && Boolean(message.body_text.trim());
    const hasMedia =
        Array.isArray(message.media) &&
        message.media.some((item) => item && typeof item === "object");
    return hasBody || hasMedia;
}

function messageCanFormRun(message) {
    return Boolean(
        !isControlTimelineMessage(message) &&
            messageHasRenderableContent(message) &&
            ["inbound", "outbound"].includes(message.direction) &&
            ["provider", "agent", "external_device"].includes(message.origin)
    );
}

/**
 * Return whether a message belongs to the compact visual run started by its
 * immediate predecessor. Invalid DTOs deliberately start a new run.
 *
 * @param {Object} message current message DTO
 * @param {Object} previous immediate predecessor DTO
 * @param {Number} windowMinutes maximum interval for a visual run
 * @returns {Boolean} whether the current message continues the prior run
 */
export function isMessageContinuation(
    message,
    previous,
    windowMinutes = MESSAGE_RUN_WINDOW_MINUTES
) {
    if (
        !messageCanFormRun(message) ||
        !messageCanFormRun(previous) ||
        message.direction !== previous.direction ||
        message.origin !== previous.origin
    ) {
        return false;
    }
    const authorKey = messageRunAuthorKey(message);
    if (!authorKey || authorKey !== messageRunAuthorKey(previous)) {
        return false;
    }
    if (!message.date || !previous.date) {
        return false;
    }
    try {
        const currentDate = deserializeDateTime(message.date);
        const previousDate = deserializeDateTime(previous.date);
        const gap = currentDate.diff(previousDate, "minutes").minutes;
        return (
            currentDate.hasSame(previousDate, "day") &&
            gap >= 0 &&
            gap < Math.max(0, windowMinutes)
        );
    } catch (_error) {
        return false;
    }
}

export function messageAuthorLabel(message) {
    const authorName =
        message &&
        message.author &&
        typeof message.author.name === "string" &&
        message.author.name.trim()
            ? message.author.name.trim()
            : "Sistema";
    if (
        message &&
        message.origin === "agent" &&
        message.author &&
        message.author.is_current_user
    ) {
        return "Você";
    }
    if (message && message.origin === "external_device") {
        return `${authorName} · outro dispositivo`;
    }
    return authorName;
}

export function groupDeliveryLabel(message) {
    const summary = message && message.group_delivery;
    if (
        !summary ||
        !Number.isSafeInteger(summary.delivered_count) ||
        summary.delivered_count < 0 ||
        !Number.isSafeInteger(summary.read_count) ||
        summary.read_count < 0
    ) {
        return "";
    }
    const labels = [];
    if (summary.read_count) {
        labels.push(
            `${summary.read_count} ${summary.read_count === 1 ? "leu" : "leram"}`
        );
    }
    if (summary.delivered_count) {
        labels.push(
            `${summary.delivered_count} ${
                summary.delivered_count === 1 ? "recebeu" : "receberam"
            }`
        );
    }
    return labels.join(" · ");
}

export function timelineViewportNearBottom(
    viewport,
    threshold = TIMELINE_BOTTOM_THRESHOLD
) {
    if (!viewport) {
        return false;
    }
    const remaining =
        viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
    return remaining <= Math.max(0, threshold);
}

export function timelineScrollDecision({
    channelChanged,
    lastMessageId,
    phase,
    preserveScroll,
    previousLastMessageId,
    wasNearBottom,
}) {
    if (phase !== "ready" || preserveScroll) {
        return "preserve";
    }
    if (channelChanged) {
        return "follow";
    }
    if (lastMessageId <= previousLastMessageId) {
        return "preserve";
    }
    return wasNearBottom ? "follow" : "notify";
}

export class ConversationTimeline extends Component {
    setup() {
        this.action = useService("action");
        this.viewportRef = useRef("viewport");
        this.editInputRef = useRef("editInput");
        this.reactionPickerRef = useRef("reactionPicker");
        this.deleteConfirmationRef = useRef("deleteConfirmation");
        this.ui = useState({
            openMenuId: false,
            reactionPickerId: false,
            editingId: false,
            editBody: "",
            deletingId: false,
            busyId: false,
            unseenMessages: 0,
            awayFromLatest: false,
        });
        this.preserveScroll = false;
        this.followLatest = true;
        this.observedChannelId = false;
        this.observedLastMessageId = 0;
        this.pendingInitialPositionChannelId = false;
        useEffect(
            () => this.synchronizeScroll(),
            () => [
                this.state.selectedChannelId,
                this.state.messages.length,
                this.state.messages.length
                    ? this.state.messages[this.state.messages.length - 1].message_id
                    : 0,
                this.state.timelinePhase,
            ]
        );
        useEffect(
            () => this.resetInteraction(),
            () => [this.state.selectedChannelId]
        );
        useEffect(
            () => this.reconcileInteractionPolicy(),
            () => [
                this.state.selectedChannelId,
                this.conversationPolicy.allow_reply,
                this.conversationPolicy.allow_react,
                this.conversationPolicy.allow_edit,
                this.conversationPolicy.allow_delete,
                this.interactionAuthorizationRevision,
            ]
        );
    }

    get state() {
        return this.props.state;
    }

    get store() {
        return this.props.store;
    }

    isForwarded(message) {
        return isForwardedTimelineMessage(message);
    }

    get conversationPolicy() {
        return conversationUiPolicy(this.store.selectedConversation);
    }

    get isGroupConversation() {
        return this.conversationPolicy.is_group;
    }

    get keepsDeletedMessageContent() {
        const conversation = this.store.selectedConversation;
        return Boolean(
            conversation &&
                conversation.account &&
                conversation.account.show_deleted_message_content === true
        );
    }

    get quickReactions() {
        return QUICK_REACTIONS;
    }

    get latestMessageId() {
        const messages = this.state.messages;
        return messages.length ? messages[messages.length - 1].message_id : 0;
    }

    initials(name) {
        return initials(name);
    }

    authorLabel(message) {
        return messageAuthorLabel(message);
    }

    messageClass(message, index) {
        const effectiveStatus = messageStatusMeta(
            message.delivery_state,
            message.dispatch_state
        );
        return [
            "cc-message",
            `cc-message--${message.direction || "internal"}`,
            isControlTimelineMessage(message) ? "cc-message--control" : "",
            this.isRunContinuation(message, index)
                ? "cc-message--run-continuation"
                : "cc-message--run-start",
            this.endsRun(index) ? "cc-message--run-end" : "",
            this.isGroupConversation && message.direction === "inbound"
                ? "cc-message--group-participant"
                : "",
            message.delivery_state === "failed" ? "is-failed" : "",
            effectiveStatus.state === "uncertain" ? "is-uncertain" : "",
        ]
            .filter(Boolean)
            .join(" ");
    }

    isRunContinuation(message, index) {
        if (!Number.isInteger(index) || index <= 0) {
            return false;
        }
        return isMessageContinuation(message, this.state.messages[index - 1]);
    }

    isControlMessage(message) {
        return isControlTimelineMessage(message);
    }

    controlAriaLabel(message) {
        const meta = controlTimelineMessageMeta(message);
        if (!meta) {
            return false;
        }
        const body =
            typeof message.body_text === "string" && message.body_text.trim()
                ? message.body_text.trim().slice(0, 240)
                : meta.fallback;
        const time = this.time(message.date);
        // Odoo 16's production JS minifier removes the whitespace inside a
        // nested template literal here. Keep the punctuation in a plain
        // string so minified and debug assets expose the same accessible name.
        return `${meta.label}: ${body}${time ? ". " + time : ""}`;
    }

    endsRun(index) {
        if (!Number.isInteger(index) || index >= this.state.messages.length - 1) {
            return true;
        }
        return !isMessageContinuation(
            this.state.messages[index + 1],
            this.state.messages[index]
        );
    }

    actionsClass(message) {
        const isOpen = [
            this.ui.openMenuId,
            this.ui.reactionPickerId,
            this.ui.deletingId,
        ].includes(message.message_id);
        return isOpen ? "cc-message-actions is-open" : "cc-message-actions";
    }

    time(value) {
        return value ? deserializeDateTime(value).toFormat("HH:mm") : "";
    }

    dayLabel(value) {
        if (!value) {
            return "";
        }
        const date = deserializeDateTime(value);
        const now = DateTime.local();
        if (date.hasSame(now, "day")) {
            return "Hoje";
        }
        if (date.hasSame(now.minus({days: 1}), "day")) {
            return "Ontem";
        }
        return formatDate(date);
    }

    startsDay(message, index) {
        if (index === 0) {
            return true;
        }
        const previous = this.state.messages[index - 1];
        if (!message.date || !previous.date) {
            return false;
        }
        return !deserializeDateTime(message.date).hasSame(
            deserializeDateTime(previous.date),
            "day"
        );
    }

    delivery(message) {
        return messageStatusMeta(message.delivery_state, message.dispatch_state);
    }

    dispatchReason(message) {
        const effectiveState = this.delivery(message).state;
        if (["sent", "delivered", "read"].includes(effectiveState)) {
            return messageDispatchReasonMeta("");
        }
        return messageDispatchReasonMeta(message && message.dispatch_reason);
    }

    get resendLabel() {
        return _t("Reenviar");
    }

    get resendingLabel() {
        return _t("Reenviando…");
    }

    groupDelivery(message) {
        return groupDeliveryLabel(message);
    }

    preview(message) {
        const current =
            message &&
            this.state.messages.find((item) => item.message_id === message.message_id);
        return messagePreviewText(current || message);
    }

    hasActions(message) {
        if (isControlTimelineMessage(message)) {
            return false;
        }
        return (
            sourceWebhookActionEnabled(this.store.capabilities, message) ||
            ["reply", "react", "edit", "delete"].some((action) =>
                this.messageActionAllowed(message, action)
            )
        );
    }

    canViewSourceWebhook(message) {
        return sourceWebhookActionEnabled(this.store.capabilities, message);
    }

    messageActionAllowed(message, action) {
        if (isControlTimelineMessage(message)) {
            return false;
        }
        return messageActionEnabled(this.store.selectedConversation, message, action);
    }

    messageById(messageId) {
        return (
            this.state.messages.find((message) => message.message_id === messageId) ||
            false
        );
    }

    get interactionAuthorizationRevision() {
        return [
            [this.ui.openMenuId, "menu"],
            [this.ui.reactionPickerId, "react"],
            [this.ui.editingId, "edit"],
            [this.ui.deletingId, "delete"],
        ]
            .map(([messageId, action]) => {
                const message = this.messageById(messageId);
                const actions = (message && message.actions) || {};
                const sourceCapability = Boolean(
                    this.store.capabilities &&
                        this.store.capabilities.view_source_webhook === true
                );
                const sourceInboxEventId =
                    message &&
                    Number.isSafeInteger(message.source_inbox_event_id) &&
                    message.source_inbox_event_id > 0
                        ? message.source_inbox_event_id
                        : 0;
                return `${messageId || 0}:${action}:${Boolean(actions.reply)}:${Boolean(
                    actions.react
                )}:${Boolean(actions.edit)}:${Boolean(
                    actions.delete
                )}:${sourceCapability}:${sourceInboxEventId}`;
            })
            .join("|");
    }

    reconcileInteractionPolicy() {
        const openMessage = this.messageById(this.ui.openMenuId);
        if (this.ui.openMenuId && !this.hasActions(openMessage)) {
            this.ui.openMenuId = false;
        }
        const reactionMessage = this.messageById(this.ui.reactionPickerId);
        if (
            this.ui.reactionPickerId &&
            !this.messageActionAllowed(reactionMessage, "react")
        ) {
            this.ui.reactionPickerId = false;
        }
        const editedMessage = this.messageById(this.ui.editingId);
        if (this.ui.editingId && !this.messageActionAllowed(editedMessage, "edit")) {
            this.cancelEdit();
        }
        const deletedMessage = this.messageById(this.ui.deletingId);
        if (
            this.ui.deletingId &&
            !this.messageActionAllowed(deletedMessage, "delete")
        ) {
            this.ui.deletingId = false;
        }
    }

    isBusy(message) {
        return this.ui.busyId === message.message_id;
    }

    closeActions() {
        this.ui.openMenuId = false;
        this.ui.reactionPickerId = false;
        this.ui.deletingId = false;
    }

    resetInteraction() {
        this.closeActions();
        this.ui.editingId = false;
        this.ui.editBody = "";
        this.ui.busyId = false;
    }

    synchronizeScroll() {
        const channelId = this.state.selectedChannelId;
        const channelChanged = channelId !== this.observedChannelId;
        const lastMessageId = this.latestMessageId;
        if (channelChanged) {
            this.observedChannelId = channelId;
            this.pendingInitialPositionChannelId = channelId;
            this.observedLastMessageId =
                this.state.timelinePhase === "ready" ? lastMessageId : 0;
            this.followLatest = true;
            this.ui.unseenMessages = 0;
            this.ui.awayFromLatest = false;
            if (this.state.timelinePhase === "ready") {
                this.scrollToInitialPosition();
            }
            return;
        }

        if (
            this.state.timelinePhase === "ready" &&
            this.pendingInitialPositionChannelId === channelId
        ) {
            this.observedLastMessageId = lastMessageId;
            this.scrollToInitialPosition();
            return;
        }

        const previousLastMessageId = this.observedLastMessageId;
        const decision = timelineScrollDecision({
            channelChanged,
            lastMessageId,
            phase: this.state.timelinePhase,
            preserveScroll: this.preserveScroll,
            previousLastMessageId,
            wasNearBottom: this.followLatest,
        });
        if (this.state.timelinePhase === "ready") {
            this.observedLastMessageId = lastMessageId;
        }
        if (decision === "follow") {
            this.scrollToBottom({markSeen: !this.state.timelineHasMoreForward});
        } else if (decision === "notify") {
            const added = this.state.messages.filter(
                (message) =>
                    message.message_id > previousLastMessageId &&
                    !(
                        message.origin === "agent" &&
                        message.author &&
                        message.author.is_current_user === true
                    )
            ).length;
            if (added) {
                this.ui.unseenMessages += added;
            }
        }
    }

    onViewportScroll() {
        this.followLatest = timelineViewportNearBottom(this.viewportRef.el);
        this.ui.awayFromLatest = !this.followLatest;
        if (this.followLatest) {
            if (this.state.timelineHasMoreForward) {
                this.loadNewer();
                return;
            }
            const shouldMarkSeen = Boolean(
                this.ui.unseenMessages || this.state.timelineFirstUnreadMessageId
            );
            this.ui.unseenMessages = 0;
            if (shouldMarkSeen && this.latestMessageId) {
                this.store.markSeen(this.latestMessageId);
            }
        }
    }

    toggleMenu(message) {
        this.ui.reactionPickerId = false;
        this.ui.deletingId = false;
        this.ui.openMenuId =
            this.ui.openMenuId === message.message_id ? false : message.message_id;
    }

    onActionKeydown(event) {
        if (event.key === "Escape") {
            event.preventDefault();
            this.closeActions();
            const toggle = event.currentTarget.querySelector(
                ".cc-message-actions__toggle"
            );
            if (toggle) {
                toggle.focus();
            }
            return;
        }
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
            return;
        }
        const items = Array.from(
            event.currentTarget.querySelectorAll('.cc-message-menu [role="menuitem"]')
        );
        if (!items.length) {
            return;
        }
        event.preventDefault();
        const current = items.indexOf(event.target);
        const last = items.length - 1;
        const next =
            event.key === "Home"
                ? 0
                : event.key === "End"
                ? last
                : event.key === "ArrowUp"
                ? current <= 0
                    ? last
                    : current - 1
                : current >= last
                ? 0
                : current + 1;
        items[next].focus();
    }

    scrollToBottom({markSeen = false} = {}) {
        const channelId = this.state.selectedChannelId;
        this.followLatest = true;
        this.ui.unseenMessages = 0;
        this.ui.awayFromLatest = false;
        browser.requestAnimationFrame(() => {
            if (channelId !== this.state.selectedChannelId) {
                return;
            }
            const viewport = this.viewportRef.el;
            if (viewport) {
                viewport.scrollTop = viewport.scrollHeight;
            }
            if (markSeen && this.latestMessageId) {
                this.store.markSeen(this.latestMessageId);
            }
        });
    }

    scrollToInitialPosition() {
        const channelId = this.state.selectedChannelId;
        this.pendingInitialPositionChannelId = false;
        const firstUnreadMessageId = this.state.timelineFirstUnreadMessageId;
        if (!Number.isSafeInteger(firstUnreadMessageId) || firstUnreadMessageId <= 0) {
            this.scrollToBottom();
            return;
        }
        this.followLatest = false;
        this.ui.unseenMessages = 0;
        this.ui.awayFromLatest = true;
        browser.requestAnimationFrame(() => {
            if (channelId !== this.state.selectedChannelId) {
                return;
            }
            const viewport = this.viewportRef.el;
            const boundary =
                viewport &&
                viewport.querySelector(
                    `[data-unread-boundary="${firstUnreadMessageId}"]`
                );
            if (!viewport || !boundary) {
                this.scrollToBottom();
                return;
            }
            viewport.scrollTop = Math.max(0, boundary.offsetTop - 16);
        });
    }

    async showLatest() {
        if (this.state.timelineHasMoreForward) {
            this.followLatest = true;
            this.ui.unseenMessages = 0;
            this.ui.awayFromLatest = false;
            await this.store.jumpToLatest();
            return;
        }
        this.scrollToBottom({markSeen: true});
    }

    async loadOlder() {
        const channelId = this.state.selectedChannelId;
        const viewport = this.viewportRef.el;
        const previousHeight = viewport ? viewport.scrollHeight : 0;
        const previousTop = viewport ? viewport.scrollTop : 0;
        this.preserveScroll = true;
        await this.store.loadOlderMessages();
        browser.requestAnimationFrame(() => {
            if (channelId === this.state.selectedChannelId && viewport) {
                viewport.scrollTop =
                    previousTop + Math.max(0, viewport.scrollHeight - previousHeight);
            }
            this.preserveScroll = false;
        });
    }

    async loadNewer() {
        const channelId = this.state.selectedChannelId;
        this.preserveScroll = true;
        await this.store.loadNewerMessages();
        browser.requestAnimationFrame(() => {
            if (channelId === this.state.selectedChannelId) {
                this.followLatest = timelineViewportNearBottom(this.viewportRef.el);
                this.ui.awayFromLatest = !this.followLatest;
            }
            this.preserveScroll = false;
        });
    }

    reply(message) {
        if (!this.messageActionAllowed(message, "reply")) {
            return;
        }
        this.closeActions();
        this.store.setReply(message);
    }

    async viewSourceWebhook(message) {
        const currentMessage = this.messageById(message && message.message_id);
        if (!this.canViewSourceWebhook(currentMessage)) {
            this.reconcileInteractionPolicy();
            return false;
        }
        const sourceInboxEventId = currentMessage.source_inbox_event_id;
        this.closeActions();
        await this.action.doAction({
            type: "ir.actions.act_window",
            name: "Webhook de origem",
            res_model: "contact.center.inbox.event",
            res_id: sourceInboxEventId,
            views: [[false, "form"]],
            view_mode: "form",
            target: "new",
        });
        return true;
    }

    openReactionPicker(message) {
        if (!this.messageActionAllowed(message, "react")) {
            return;
        }
        this.ui.openMenuId = false;
        this.ui.deletingId = false;
        this.ui.reactionPickerId =
            this.ui.reactionPickerId === message.message_id
                ? false
                : message.message_id;
        if (this.ui.reactionPickerId) {
            browser.requestAnimationFrame(() => {
                const first =
                    this.reactionPickerRef.el &&
                    this.reactionPickerRef.el.querySelector("button");
                if (first) {
                    first.focus();
                }
            });
        }
    }

    reactionOperation(message, emoji) {
        return (message.reactions || []).some(
            (reaction) => reaction.emoji === emoji && reaction.reacted_by_me
        )
            ? "remove"
            : "add";
    }

    reactionLabel(message, reaction) {
        const action = reaction.reacted_by_me ? "Remover" : "Adicionar";
        return `${action} reação ${reaction.emoji}; ${reaction.count}`;
    }

    async react(message, emoji) {
        if (this.isBusy(message) || !this.messageActionAllowed(message, "react")) {
            return;
        }
        this.ui.busyId = message.message_id;
        const success = await this.store.reactMessage(
            message.message_id,
            emoji,
            this.reactionOperation(message, emoji)
        );
        if (success) {
            this.ui.reactionPickerId = false;
        }
        this.ui.busyId = false;
    }

    startEdit(message) {
        if (!this.messageActionAllowed(message, "edit")) {
            return;
        }
        this.closeActions();
        this.ui.editingId = message.message_id;
        this.ui.editBody = message.body_text || "";
        browser.requestAnimationFrame(() => {
            const input = this.editInputRef.el;
            if (input) {
                input.focus();
                input.setSelectionRange(input.value.length, input.value.length);
            }
        });
    }

    cancelEdit() {
        this.ui.editingId = false;
        this.ui.editBody = "";
    }

    onEditInput(event) {
        this.ui.editBody = event.target.value;
    }

    onEditKeydown(event, message) {
        if (event.key === "Escape") {
            event.preventDefault();
            this.cancelEdit();
        } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            this.saveEdit(message);
        }
    }

    async saveEdit(message) {
        const body = this.ui.editBody.trim();
        if (
            !this.messageActionAllowed(message, "edit") ||
            !body ||
            body === (message.body_text || "").trim() ||
            this.isBusy(message)
        ) {
            return;
        }
        this.ui.busyId = message.message_id;
        const success = await this.store.editMessage(message.message_id, body);
        this.ui.busyId = false;
        if (success) {
            this.cancelEdit();
        }
    }

    askDelete(message) {
        if (!this.messageActionAllowed(message, "delete")) {
            return;
        }
        this.ui.openMenuId = false;
        this.ui.reactionPickerId = false;
        this.ui.deletingId = message.message_id;
        browser.requestAnimationFrame(() => {
            const first =
                this.deleteConfirmationRef.el &&
                this.deleteConfirmationRef.el.querySelector("button");
            if (first) {
                first.focus();
            }
        });
    }

    async deleteMessage(message) {
        if (this.isBusy(message) || !this.messageActionAllowed(message, "delete")) {
            return;
        }
        this.ui.busyId = message.message_id;
        const success = await this.store.deleteMessage(message.message_id);
        this.ui.busyId = false;
        if (success) {
            this.closeActions();
        }
    }

    async resend(message) {
        if (this.isBusy(message) || !this.messageActionAllowed(message, "resend")) {
            return false;
        }
        this.closeActions();
        this.ui.busyId = message.message_id;
        const success = await this.store.resendMessage(message.message_id);
        this.ui.busyId = false;
        return success;
    }
}

ConversationTimeline.components = {MessageContent};
ConversationTimeline.props = {state: Object, store: Object};
ConversationTimeline.template = "contact_center_ui.ConversationTimeline";
