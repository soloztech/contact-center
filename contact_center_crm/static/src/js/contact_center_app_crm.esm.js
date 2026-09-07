/** @odoo-module **/

import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";
import {CrmPanel} from "./crm_panel.esm";
import {patch} from "@web/core/utils/patch";
import {useState} from "@odoo/owl";

patch(ContactCenterApp, "contact_center_crm.components", {
    components: {...ContactCenterApp.components, CrmPanel},
});

patch(ContactCenterApp.prototype, "contact_center_crm.customer_records", {
    setup() {
        this._super(...arguments);
        this.crmUi = useState({panelMode: "contact"});
    },

    get canViewCrm() {
        return this.store.capabilities.view_crm === true;
    },

    get crmPanelSelected() {
        return this.canViewCrm && this.crmUi.panelMode === "crm";
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
        if (this.crmPanelSelected && this.store.state.detailsOpen) {
            this.store.state.detailsOpen = false;
        } else {
            this.crmUi.panelMode = "crm";
            this.store.state.detailsOpen = true;
        }
    },

    toggleContactPanel() {
        if (this.crmPanelSelected) {
            this.crmUi.panelMode = "contact";
            this.store.state.detailsOpen = true;
        } else {
            this.store.toggleDetails();
        }
    },

    closeCrmPanel() {
        this.store.state.detailsOpen = false;
        const trigger = document.querySelector(".o_contact_center_ui .cc-crm-toggle");
        if (trigger) {
            trigger.focus();
        }
    },
});
