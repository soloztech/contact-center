/** @odoo-module **/

import {Component, onWillDestroy, onWillStart, useState} from "@odoo/owl";
import {
    connectionFleetMeta,
    conversationAvatarUrl,
    conversationDisplayName,
    conversationResolutionAction,
    conversationUiPolicy,
    initials,
} from "./contact_center_model.esm";
import {BrowserAttention} from "./browser_attention.esm";
import {ContactCenterStore} from "./contact_center_store.esm";
import {ContactPanel} from "./contact_panel.esm";
import {ConversationList} from "./conversation_list.esm";
import {ConversationTimeline} from "./conversation_timeline.esm";
import {DeferredImage} from "./deferred_image.esm";
import {MessageComposer} from "./message_composer.esm";
import {browser} from "@web/core/browser/browser";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";

export function conversationComposerAvailable(policy, capabilities) {
    return Boolean(
        (policy && policy.show_composer === true) ||
            (capabilities && capabilities.internal_notes === true)
    );
}

export class ContactCenterApp extends Component {
    setup() {
        this.ui = useState({stateChanging: false, failedHeaderAvatarUrl: false});
        this.attention = new BrowserAttention();
        this.store = new ContactCenterStore({
            orm: useService("orm"),
            busService: useService("bus_service"),
            notification: useService("notification"),
            stateFactory: useState,
            attention: this.attention,
            initialActionParams: this.props.action && this.props.action.params,
        });
        this.mobileHealthFocusTimer = null;
        // Do not return the promise: the initial loading state is a real skeleton,
        // while bootstrap and the bus start concurrently in the background.
        onWillStart(() => {
            this.store.start();
        });
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

    get selectedConversation() {
        return this.store.selectedConversation;
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
    DeferredImage,
    MessageComposer,
};
ContactCenterApp.template = "contact_center_ui.ContactCenterApp";

registry.category("actions").add("contact_center_ui.inbox", ContactCenterApp);
