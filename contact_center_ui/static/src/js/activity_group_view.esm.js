/** @odoo-module **/

import {attr} from "@mail/model/model_field";
import {registerPatch} from "@mail/model/model_core";

export function contactCenterActivityAction(filter) {
    const timing = {
        my: "due",
        overdue: "overdue",
        today: "today",
        upcoming_all: "planned",
        summary: "all",
    }[filter];
    return {
        type: "ir.actions.client",
        tag: "contact_center_ui.inbox",
        name: "Contact Center",
        params: {activity_timing: timing || "due"},
    };
}

export function openContactCenterActivityGroup(view, event, summary = false) {
    if (!view.activityGroup.contactCenter) {
        return false;
    }
    event.stopPropagation();
    view.activityMenuViewOwner.update({isOpen: false});
    const button = event.target.closest("[data-filter]");
    const filter = summary ? "summary" : (button && button.dataset.filter) || "my";
    view.env.services.action.doAction(contactCenterActivityAction(filter), {
        clearBreadcrumbs: true,
    });
    return true;
}

registerPatch({
    name: "ActivityGroup",
    fields: {contactCenter: attr({default: false})},
    modelMethods: {
        convertData(data) {
            return {...this._super(data), contactCenter: data.contact_center === true};
        },
    },
});

registerPatch({
    name: "ActivityGroupView",
    recordMethods: {
        onClick(event) {
            if (!openContactCenterActivityGroup(this, event, true)) {
                return this._super(...arguments);
            }
        },
        onClickFilterButton(event) {
            if (!openContactCenterActivityGroup(this, event)) {
                return this._super(...arguments);
            }
        },
    },
});
