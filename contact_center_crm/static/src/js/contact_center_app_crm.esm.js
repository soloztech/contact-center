/** @odoo-module **/

import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";
import {CrmPanel} from "./crm_panel.esm";
import {patch} from "@web/core/utils/patch";

patch(ContactCenterApp, "contact_center_crm.components", {
    components: {...ContactCenterApp.components, CrmPanel},
});

patch(ContactCenterApp.prototype, "contact_center_crm.customer_records", {
    get canViewCrm() {
        return this.store.capabilities.view_crm === true;
    },

    get crmPanelSelected() {
        return this.canViewCrm && this.ui.sidePanel === "crm";
    },

    get crmPanelKey() {
        const conversation = this.selectedConversation;
        if (!conversation) {
            return "none";
        }
        const partner = conversation.identity && conversation.identity.partner;
        const company = partner && partner.company;
        return `${conversation.channel_id}:${partner ? partner.id : 0}:${
            company ? company.id : 0
        }`;
    },

    toggleCrmPanel() {
        if (!this.canViewCrm) {
            return;
        }
        this.toggleSidePanel("crm");
    },

    closeCrmPanel() {
        this.store.state.detailsOpen = false;
        const trigger = document.querySelector(".o_contact_center_ui .cc-crm-toggle");
        if (trigger) {
            trigger.focus();
        }
    },
});
