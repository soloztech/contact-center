/** @odoo-module **/

import {browser} from "@web/core/browser/browser";

export const AUTOMATIC_READ_DEADLINE = 30000;

export class AutomaticRefreshDeferred extends Error {
    constructor(message = "Atualização automática adiada. Tentaremos novamente.") {
        super(message);
        this.name = "AutomaticRefreshDeferred";
    }
}

/** One running cycle and one dirty bit, with fixed leading debounce. */
export class CoalescedRefresh {
    constructor({
        run,
        timer = browser,
        now = () => performance.now(),
        random = Math.random,
        debounce = 120,
        interval = 2000,
        canRun = () => true,
        retryOnDeadline = true,
    }) {
        Object.assign(this, {
            callback: run,
            timerApi: timer,
            now,
            random,
            debounce,
            interval,
            canRun,
            retryOnDeadline,
        });
        this.timer = null;
        this.timerAt = 0;
        this.pending = false;
        this.urgent = false;
        this.running = false;
        this.destroyed = false;
        this.lastStarted = -Infinity;
        this.blockedUntil = 0;
        this.deadlines = 0;
        this.deadlineRevision = 0;
    }

    schedule({urgent = false} = {}) {
        if (this.destroyed) {
            return false;
        }
        this.pending = true;
        this.urgent = this.urgent || urgent;
        if (
            urgent &&
            this.timer !== null &&
            this.timerAt > Math.max(this.now() + this.debounce, this.blockedUntil)
        ) {
            this.pause();
        }
        this.arm();
        return true;
    }

    arm() {
        if (
            this.destroyed ||
            this.running ||
            this.timer !== null ||
            !this.pending ||
            !this.canRun()
        ) {
            return;
        }
        const delay = Math.ceil(
            Math.max(
                this.debounce,
                this.blockedUntil - this.now(),
                this.urgent ? 0 : this.lastStarted + this.interval - this.now()
            )
        );
        this.timerAt = this.now() + delay;
        this.timer = this.timerApi.setTimeout(() => {
            this.timer = null;
            return this.run();
        }, delay);
    }

    async run() {
        if (this.destroyed) {
            return false;
        }
        if (
            this.running ||
            !this.canRun() ||
            this.now() < this.blockedUntil ||
            (!this.urgent && this.now() < this.lastStarted + this.interval)
        ) {
            this.schedule();
            return false;
        }
        if (this.timer !== null) {
            this.timerApi.clearTimeout(this.timer);
            this.timer = null;
        }
        this.pending = false;
        this.urgent = false;
        this.running = true;
        this.lastStarted = this.now();
        const revision = this.deadlineRevision;
        try {
            const result = await this.callback();
            if (result === true && revision === this.deadlineRevision) {
                this.deadlines = 0;
                this.blockedUntil = 0;
            }
            return result;
        } catch (_error) {
            // Background work must never produce an unhandled rejection.
            return false;
        } finally {
            this.running = false;
            this.arm();
        }
    }

    noteDeadline() {
        this.deadlineRevision += 1;
        const delay = Math.min(120000, 30000 * 2 ** Math.min(this.deadlines++, 2));
        this.blockedUntil =
            this.now() +
            Math.ceil(
                delay * (1 + Math.max(0.001, Math.min(0.1, this.random() * 0.1)))
            );
        if (this.retryOnDeadline) {
            this.schedule();
        } else {
            // This lane waits for a new trigger after a failed preview read.
            this.pending = false;
            this.urgent = false;
            this.pause();
        }
    }

    pause() {
        if (this.timer !== null) {
            this.timerApi.clearTimeout(this.timer);
            this.timer = null;
        }
    }

    destroy() {
        this.destroyed = true;
        this.pending = false;
        this.pause();
    }
}

/** Own the actual abortable ORM request, not an unbounded Promise.race. */
export class AutomaticReadOwner {
    constructor({timer = browser, onDeadline = () => undefined} = {}) {
        this.timer = timer;
        this.onDeadline = onDeadline;
        this.requests = new Set();
        this.destroyed = false;
    }

    read(request) {
        if (this.destroyed) {
            if (request.abort) {
                request.abort();
            }
            // Consume the abort rejection even when destroyed before registration.
            Promise.resolve(request).catch(() => undefined);
            return Promise.reject(new AutomaticRefreshDeferred());
        }
        return new Promise((resolve, reject) => {
            const entry = {request, timer: null, cancel: null};
            let settled = false;
            const finish = (callback, value) => {
                if (settled) {
                    return;
                }
                settled = true;
                this.timer.clearTimeout(entry.timer);
                this.requests.delete(entry);
                callback(value);
            };
            entry.cancel = () => {
                finish(reject, new AutomaticRefreshDeferred());
                if (request.abort) {
                    request.abort();
                }
            };
            entry.timer = this.timer.setTimeout(() => {
                this.onDeadline();
                entry.cancel();
            }, AUTOMATIC_READ_DEADLINE);
            this.requests.add(entry);
            Promise.resolve(request).then(
                (value) => finish(resolve, value),
                (error) => finish(reject, error)
            );
        });
    }

    destroy() {
        this.destroyed = true;
        for (const entry of [...this.requests]) {
            entry.cancel();
        }
    }
}
