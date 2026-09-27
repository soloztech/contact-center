/** @odoo-module **/

import {Component, onWillDestroy, onWillStart, useEffect, useState} from "@odoo/owl";
import {ContactCenterStore, consumeInboxActionParams} from "./contact_center_store.esm";
import {
    connectionFleetMeta,
    conversationAvatarUrl,
    conversationDisplayName,
    conversationResolutionAction,
    conversationUiPolicy,
    initials,
    technicalChannelActionEnabled,
} from "./contact_center_model.esm";
import {useOwnedDialogs, useService} from "@web/core/utils/hooks";
import {BrowserAttention} from "./browser_attention.esm";
import {ContactPanel} from "./contact_panel.esm";
import {ConversationList} from "./conversation_list.esm";
import {ConversationResolution} from "./conversation_resolution.esm";
import {ConversationTags} from "./conversation_tags.esm";
import {ConversationTimeline} from "./conversation_timeline.esm";
import {DeferredImage} from "./deferred_image.esm";
import {MessageComposer} from "./message_composer.esm";
import {browser} from "@web/core/browser/browser";
import {_t} from "@web/core/l10n/translation";
import {registry} from "@web/core/registry";
import {useSetupAction} from "@web/webclient/actions/action_hook";

export function conversationComposerAvailable(policy, capabilities) {
    return Boolean(
        (policy && policy.show_composer === true) ||
            (capabilities && capabilities.internal_notes === true)
    );
}

function browserLocalStorage() {
    try {
        return browser.localStorage;
    } catch (_error) {
        return false;
    }
}

export class ContactCenterApp extends Component {
    setup() {
        this.ui = useState({
            stateChanging: false,
            failedHeaderAvatarUrl: false,
            sidePanel: "contact",
        });
        this.addDialog = useOwnedDialogs();
        this.action = useService("action");
        this.attention = new BrowserAttention();
        const inboxContext = useService("contact_center_ui.inbox_context");
        this.store = new ContactCenterStore({
            orm: useService("orm"),
            busService: useService("bus_service"),
            notification: useService("notification"),
            stateFactory: useState,
            attention: this.attention,
            // A breadcrumb return reuses the action: its parameters apply once.
            initialActionParams: consumeInboxActionParams(
                inboxContext,
                this.props.action
            ),
            inboxPreferenceStorage: browserLocalStorage(),
            inboxContext,
        });
        this.layoutRestored = false;
        useEffect(
            (phase) => {
                if (phase === "ready" && !this.layoutRestored) {
                    // Saved after the bootstrap; a CRM panel needs its right.
                    this.layoutRestored = true;
                    const saved = this.store.inboxLayout.sidePanel;
                    this.ui.sidePanel =
                        saved === "crm" && !this.canViewCrm ? "contact" : saved;
                }
            },
            () => [this.store.state.phase]
        );
        this.mobileHealthFocusTimer = null;
        // Do not return the promise: the initial loading state is a real skeleton,
        // while bootstrap and the bus start concurrently in the background.
        onWillStart(() => {
            this.store.start();
        });
        // The next action may boot its inbox before this one is destroyed:
        // hand the visit over while leaving, before it starts.
        useSetupAction({beforeLeave: () => this.store.handOffDocument()});
        onWillDestroy(() => {
            if (this.mobileHealthFocusTimer !== null) {
                browser.clearTimeout(this.mobileHealthFocusTimer);
            }
            this.store.destroy();
        });
    }

    get shellClass() {
        const classes = [
            "o_action",
            "o_contact_center_ui",
            `cc-mobile-pane--${this.store.state.mobilePane}`,
        ];
        if (this.store.state.detailsOpen) {
            classes.push("cc-details-open");
        }
        if (this.store.state.selectedChannelId) {
            classes.push("cc-has-selection");
        }
        if (this.store.state.inboxDensity === "compact") {
            classes.push("cc-inbox-compact");
        }
        return classes.join(" ");
    }

    get canViewTechnicalChannel() {
        return technicalChannelActionEnabled(
            this.store.capabilities,
            this.selectedConversation
        );
    }

    async viewTechnicalChannel() {
        if (!this.canViewTechnicalChannel) {
            return false;
        }
        await this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("Canal técnico"),
            res_model: "mail.channel",
            res_id: this.selectedConversation.channel_id,
            views: [[false, "form"]],
            view_mode: "form",
            target: "new",
        });
        return true;
    }

    get selectedConversation() {
        return this.store.selectedConversation;
    }

    get contactPanelSelected() {
        return this.ui.sidePanel === "contact";
    }

    rememberLayout() {
        this.store.rememberInboxLayout({
            sidePanel: this.ui.sidePanel,
            detailsOpen: Boolean(this.store.state.detailsOpen),
        });
    }

    onConversationInteraction(event) {
        // Clicking, typing, scrolling or focusing the composer is the agent's
        // consent to read a conversation restored on return. Other focus moves
        // into the conversation may be programmatic (closing a side panel).
        if (
            event &&
            event.type === "focusin" &&
            !(event.target instanceof Element && event.target.closest(".cc-composer"))
        ) {
            return;
        }
        this.store.resumeSeen();
    }

    toggleSidePanel(name) {
        const wasOpen = this.ui.sidePanel === name && this.store.state.detailsOpen;
        this.ui.sidePanel = name;
        this.store.state.detailsOpen = !wasOpen;
        this.rememberLayout();
    }

    toggleContactPanel() {
        if (!this.contactPanelSelected) {
            this.ui.sidePanel = "contact";
            this.store.state.detailsOpen = false;
        }
        this.store.toggleDetails();
        this.rememberLayout();
    }

    get retentionIndicator() {
        const policy = this.selectedConversation && this.selectedConversation.retention;
        return policy &&
            policy.effective === true &&
            Number.isSafeInteger(policy.days) &&
            policy.days > 0
            ? `Histórico: ${policy.days} ${policy.days === 1 ? "dia" : "dias"}`
            : "";
    }

    openRetentionPanel() {
        this.ui.sidePanel = "contact";
        this.store.state.detailsOpen = true;
        this.store.state.retentionFocusRequest += 1;
        this.rememberLayout();
    }

    get conversationPolicy() {
        return conversationUiPolicy(this.selectedConversation);
    }

    get canShowComposer() {
        return conversationComposerAvailable(
            this.conversationPolicy,
            this.store.capabilities
        );
    }

    get selectedConversationName() {
        return conversationDisplayName(this.selectedConversation);
    }

    get selectedConversationAvatarUrl() {
        return conversationAvatarUrl(this.selectedConversation);
    }

    get selectedConversationInitials() {
        return initials(this.selectedConversationName);
    }

    get selectedInboxName() {
        const account = this.selectedConversation && this.selectedConversation.account;
        return account && typeof account.name === "string" && account.name.trim()
            ? account.name.trim().slice(0, 160)
            : "Caixa não identificada";
    }

    get resolutionAction() {
        return conversationResolutionAction(this.selectedConversation);
    }

    get fleetMeta() {
        return connectionFleetMeta(this.store.connectionHealth);
    }

    get mobileFleetLabel() {
        const summary = this.store.connectionHealth.summary;
        return `${summary.connected}/${summary.total}`;
    }

    get fleetAriaLabel() {
        return `Saúde das conexões: ${this.fleetMeta.label}`;
    }

    openConnectionHealth() {
        this.store.state.connectionHealthOpen = true;
        this.store.showConversationList();
        this.mobileHealthFocusTimer = browser.setTimeout(() => {
            this.mobileHealthFocusTimer = null;
            const trigger = document.querySelector(
                ".o_contact_center_ui .cc-connection-health__trigger"
            );
            if (trigger) {
                trigger.focus();
            }
        }, 0);
    }

    async applyResolutionAction() {
        const action = this.resolutionAction;
        if (!action || this.ui.stateChanging) {
            return false;
        }
        this.ui.stateChanging = true;
        if (action.target === "resolved") {
            this.addDialog(
                ConversationResolution,
                {store: this.store, conversation: this.selectedConversation},
                {
                    onClose: () => {
                        this.ui.stateChanging = false;
                    },
                }
            );
            return true;
        }
        try {
            return await this.store.setConversationState(action.target);
        } finally {
            this.ui.stateChanging = false;
        }
    }
}

ContactCenterApp.components = {
    ContactPanel,
    ConversationList,
    ConversationTimeline,
    ConversationTags,
    DeferredImage,
    MessageComposer,
};
ContactCenterApp.template = "contact_center_ui.ContactCenterApp";

registry.category("actions").add("contact_center_ui.inbox", ContactCenterApp);
