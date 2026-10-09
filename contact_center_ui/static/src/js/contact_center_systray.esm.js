/** @odoo-module **/

import {AutomaticReadOwner, CoalescedRefresh} from "./contact_center_refresh.esm";
import {browser} from "@web/core/browser/browser";
import {
    CONNECTION_HEALTH_EVENT_TYPE,
    contactCenterNotifications,
    conversationUpdateScope,
} from "./contact_center_model.esm";

export const SYSTRAY_SUMMARY_DEBOUNCE = 800;
export const SYSTRAY_SAFETY_REFRESH = 5 * 60 * 1000;

// Any message counts: the Odoo unread counter includes messages sent by
// another agent, not only the customer's. Muting and deletion change the
// counted set without a new message; an internal note or a completed
// follow-up (message_updated, productivity_updated) advances the author's
// read pointer without a member_seen event.
// Unknown event names keep the conservative recount path. Only known events
// that cannot affect the current user's unread summary are excluded.
const NON_REFRESHING_EVENTS = new Set([
    CONNECTION_HEALTH_EVENT_TYPE,
    "delivery_updated",
    "identity_updated",
    "reaction_updated",
    "media_updated",
    "attribution_updated",
    "member_fetched",
]);

/**
 * Unread summary of the top bar, independent from the Central's store.
 *
 * The server counts conversations with the exact filters of the presets, so
 * the numbers shown are the conversations each preset opens.
 */
export class SystraySummary {
    constructor({
        call,
        userId,
        timer = browser,
        now = () => performance.now(),
        random = Math.random,
        onChange = () => undefined,
    }) {
        this.call = call;
        this.userId = userId;
        this.timer = timer;
        this.onChange = onChange;
        this.refresh = new CoalescedRefresh({
            timer,
            now,
            random,
            debounce: SYSTRAY_SUMMARY_DEBOUNCE,
            run: () => this.fetchOnce(),
        });
        this.reads = new AutomaticReadOwner({
            timer,
            onDeadline: () => this.refresh.noteDeadline(),
        });
        this.safetyTimer = null;
        this.destroyed = false;
        this.state = {enabled: false, mine: 0, all: 0};
    }

    start() {
        this.load();
        this.armSafetyRefresh();
    }

    destroy() {
        this.destroyed = true;
        this.refresh.destroy();
        this.reads.destroy();
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
                (payload.event_type !== "member_seen" &&
                    !NON_REFRESHING_EVENTS.has(payload.event_type) &&
                    !conversationUpdateScope(payload)) ||
                (payload.event_type === "member_seen" &&
                    payload.user_id === this.userId)
        );
        return relevant ? this.schedule() : false;
    }

    schedule() {
        return this.refresh.schedule();
    }

    load() {
        return this.refresh.run();
    }

    async fetchOnce() {
        let payload = null;
        try {
            payload = await this.reads.read(this.call());
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
