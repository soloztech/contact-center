/** @odoo-module **/

import {Component, onWillDestroy, onWillStart, useState} from "@odoo/owl";
import {Dropdown} from "@web/core/dropdown/dropdown";
import {DropdownItem} from "@web/core/dropdown/dropdown_item";
import {browser} from "@web/core/browser/browser";
import {contactCenterNotifications} from "./contact_center_model.esm";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";

export const SYSTRAY_SUMMARY_DEBOUNCE = 800;
export const SYSTRAY_SAFETY_REFRESH = 5 * 60 * 1000;

// Any message counts: the Odoo unread counter includes messages sent by
// another agent, not only the customer's. Muting and deletion change the
// counted set without a new message; an internal note or a completed
// follow-up (message_updated, productivity_updated) advances the author's
// read pointer without a member_seen event.
const REFRESHING_EVENTS = new Set([
    "message_created",
    "message_updated",
    "message_deleted",
    "productivity_updated",
    "conversation_updated",
    "conversation_deleted",
    "conversation_preference_updated",
]);

export function systrayInboxAction(preset = false) {
    return {
        type: "ir.actions.client",
        tag: "contact_center_ui.inbox",
        name: "Contact Center",
        params: preset ? {preset} : {},
    };
}

/**
 * Unread summary of the top bar, independent from the Central's store.
 *
 * The server counts conversations with the exact filters of the presets, so
 * the numbers shown are the conversations each preset opens.
 */
export class SystraySummary {
    constructor({call, userId, timer = browser, onChange = () => undefined}) {
        this.call = call;
        this.userId = userId;
        this.timer = timer;
        this.onChange = onChange;
        this.debounceTimer = null;
        this.safetyTimer = null;
        this.loading = false;
        this.reloadPending = false;
        this.destroyed = false;
        this.state = {enabled: false, mine: 0, all: 0};
    }

    start() {
        this.load();
        this.armSafetyRefresh();
    }

    destroy() {
        this.destroyed = true;
        if (this.debounceTimer !== null) {
            this.timer.clearTimeout(this.debounceTimer);
            this.debounceTimer = null;
        }
        if (this.safetyTimer !== null) {
            this.timer.clearTimeout(this.safetyTimer);
            this.safetyTimer = null;
        }
    }

    armSafetyRefresh() {
        if (this.destroyed) {
            return;
        }
        this.safetyTimer = this.timer.setTimeout(() => {
            this.safetyTimer = null;
            this.schedule();
            this.armSafetyRefresh();
        }, SYSTRAY_SAFETY_REFRESH);
    }

    /**
     * Schedule one reload for the notifications that may change the counts.
     *
     * @param {Array} detail bus notifications
     * @returns {Boolean} whether a reload was scheduled
     */
    handleNotifications(detail) {
        const relevant = contactCenterNotifications(detail).some(
            (payload) =>
                REFRESHING_EVENTS.has(payload.event_type) ||
                (payload.event_type === "member_seen" &&
                    payload.user_id === this.userId)
        );
        return relevant ? this.schedule() : false;
    }

    schedule() {
        if (this.destroyed) {
            return false;
        }
        if (this.debounceTimer !== null) {
            return true;
        }
        this.debounceTimer = this.timer.setTimeout(() => {
            this.debounceTimer = null;
            this.load();
        }, SYSTRAY_SUMMARY_DEBOUNCE);
        return true;
    }

    /**
     * Load the summary with one request at a time.
     *
     * Events arriving meanwhile are coalesced into one more request after it,
     * so sustained traffic cannot keep discarding every answer.
     *
     * @returns {Promise<Boolean>} whether counts were applied
     */
    async load() {
        if (this.loading) {
            this.reloadPending = true;
            return false;
        }
        this.loading = true;
        let applied = false;
        try {
            do {
                this.reloadPending = false;
                applied = (await this.fetchOnce()) || applied;
            } while (this.reloadPending && !this.destroyed);
        } finally {
            this.loading = false;
        }
        return applied;
    }

    async fetchOnce() {
        let payload = null;
        try {
            payload = await this.call();
        } catch (_error) {
            // The top bar never interrupts the agent; the next event retries.
            return false;
        }
        if (this.destroyed) {
            return false;
        }
        const count = (value) => (Number.isSafeInteger(value) && value > 0 ? value : 0);
        this.state =
            payload && payload.enabled === true
                ? {
                      enabled: true,
                      mine: count(payload.mine_unread),
                      all: count(payload.all_unread),
                  }
                : {enabled: false, mine: 0, all: 0};
        this.onChange(this.state);
        return true;
    }
}

export class ContactCenterSystray extends Component {
    setup() {
        this.action = useService("action");
        const orm = useService("orm");
        const busService = useService("bus_service");
        const user = useService("user");
        this.state = useState({enabled: false, mine: 0, all: 0});
        this.summary = new SystraySummary({
            // A background refresh: no loading indicator, never blocks the UI.
            call: () => orm.silent.call("contact.center.ui.api", "systray_summary", []),
            userId: user.userId,
            onChange: (value) => Object.assign(this.state, value),
        });
        const onNotification = (event) =>
            this.summary.handleNotifications(event && event.detail);
        const onFocus = () => this.summary.schedule();
        onWillStart(() => {
            // Never delay the web client: the counts arrive when ready.
            this.summary.start();
            busService.addEventListener("notification", onNotification);
            browser.addEventListener("focus", onFocus);
        });
        onWillDestroy(() => {
            busService.removeEventListener("notification", onNotification);
            browser.removeEventListener("focus", onFocus);
            this.summary.destroy();
        });
    }

    open(preset) {
        return this.action.doAction(systrayInboxAction(preset), {
            clearBreadcrumbs: true,
        });
    }
}

ContactCenterSystray.template = "contact_center_ui.ContactCenterSystray";
ContactCenterSystray.components = {Dropdown, DropdownItem};

registry
    .category("systray")
    .add(
        "contact_center_ui.ContactCenterSystray",
        {Component: ContactCenterSystray},
        {sequence: 25}
    );
