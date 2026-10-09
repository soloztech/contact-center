/** @odoo-module **/

import {
    CONNECTION_HEALTH_EVENT_TYPE,
    CONTACT_CENTER_NOTIFICATION_TYPE,
    SUPPORTED_SCHEMA_VERSION,
} from "./contact_center_model.esm";
import {AutomaticRefreshDeferred} from "./contact_center_refresh.esm";
import {registry} from "@web/core/registry";

const HEALTH_MODEL = "contact.center.ui.api";
const HEALTH_METHOD = "get_connection_health";

/**
 * Canonical JSON only: no getters, cycles, holes, symbols or class instances.
 *
 * @param {*} value input value
 * @param {Set} ancestors the current recursion path
 * @returns {String} canonical serialized JSON
 */
export function canonicalSharedReadJson(value, ancestors = new Set()) {
    if (value === null || typeof value === "string" || typeof value === "boolean") {
        return JSON.stringify(value);
    }
    if (typeof value === "number" && Number.isFinite(value)) {
        return JSON.stringify(value);
    }
    if (typeof value !== "object" || ancestors.has(value)) {
        throw new TypeError("A shared read requires plain JSON values.");
    }
    const array = Array.isArray(value);
    const prototype = Object.getPrototypeOf(value);
    if (
        (array && prototype !== Array.prototype) ||
        (!array && prototype !== Object.prototype && prototype !== null)
    ) {
        throw new TypeError("A shared read requires plain JSON objects.");
    }
    const keys = Reflect.ownKeys(value);
    if (
        keys.some((key) => typeof key !== "string") ||
        (array && keys.length !== value.length + 1)
    ) {
        throw new TypeError("A shared read requires plain JSON properties.");
    }
    ancestors.add(value);
    try {
        const read = (key) => {
            const property = Object.getOwnPropertyDescriptor(value, key);
            if (!property || !property.enumerable || !("value" in property)) {
                throw new TypeError("A shared read requires plain JSON properties.");
            }
            return canonicalSharedReadJson(property.value, ancestors);
        };
        if (array) {
            return `[${Array.from({length: value.length}, (_, index) =>
                read(String(index))
            ).join(",")}]`;
        }
        return `{${keys
            .sort()
            .map((key) => `${JSON.stringify(key)}:${read(key)}`)
            .join(",")}}`;
    } finally {
        ancestors.delete(value);
    }
}

/** An intratab pool of running reads; settled values are never retained. */
export class SharedReadPool {
    constructor({orm, getContext = () => ({})}) {
        this.orm = orm;
        this.getContext = getContext;
        this.entries = new Map();
        // Retired causal generations still have subscribers until they settle.
        this.running = new Set();
        this.generations = new Map();
        this.epoch = 0;
        this.contextSignature = undefined;
        this.destroyed = false;
    }

    checkContext() {
        let signature = null;
        try {
            signature = canonicalSharedReadJson(this.getContext());
        } catch (_error) {
            // A context that cannot be represented exactly cannot share reads.
        }
        if (signature !== this.contextSignature) {
            this.contextSignature = signature;
            this.epoch += 1;
            for (const entry of [...this.running]) {
                this.cancelEntry(entry);
            }
        }
        return this.epoch;
    }

    invalidate(model = HEALTH_MODEL, method = HEALTH_METHOD) {
        const methodKey = `${model}/${method}`;
        this.generations.set(methodKey, (this.generations.get(methodKey) || 0) + 1);
        for (const [key, entry] of this.entries) {
            if (entry.methodKey === methodKey) {
                this.entries.delete(key);
            }
        }
    }

    call(model, method, args = [], kwargs = {}) {
        if (model !== HEALTH_MODEL || method !== HEALTH_METHOD) {
            const refused = Promise.reject(
                new TypeError(
                    "Shared reads only allow contact.center.ui.api/get_connection_health."
                )
            );
            refused.catch(() => undefined);
            refused.abort = () => undefined;
            return refused;
        }
        this.checkContext();
        let key = null;
        const methodKey = `${model}/${method}`;
        if (!this.destroyed && this.contextSignature !== null) {
            try {
                key = canonicalSharedReadJson({
                    model,
                    method,
                    args,
                    kwargs,
                    context: this.contextSignature,
                    epoch: this.epoch,
                    generation: this.generations.get(methodKey) || 0,
                });
            } catch (_error) {
                // Unsupported input takes a separate transport, without sharing.
            }
        }
        let entry = key === null ? null : this.entries.get(key);
        let fresh = false;
        if (!entry) {
            fresh = true;
            entry = {
                key,
                methodKey,
                epoch: this.epoch,
                subscribers: new Set(),
                request: null,
                settled: false,
                aborted: false,
            };
            this.running.add(entry);
            if (key !== null) {
                this.entries.set(key, entry);
            }
        }
        const promise = this.subscribe(entry);
        if (this.destroyed) {
            this.cancelEntry(entry);
        } else if (fresh) {
            try {
                entry.request = this.orm.call(model, method, args, kwargs);
                Promise.resolve(entry.request).then(
                    (value) => this.settle(entry, null, value),
                    (error) => this.settle(entry, error, undefined, true)
                );
            } catch (error) {
                this.settle(entry, error, undefined, true);
            }
        }
        return promise;
    }

    subscribe(entry) {
        let subscriber = null;
        const promise = new Promise((resolve, reject) => {
            subscriber = {resolve, reject};
            entry.subscribers.add(subscriber);
        });
        // Aborting without observing the returned promise must also be harmless.
        promise.catch(() => undefined);
        promise.abort = (...options) => {
            if (!entry.subscribers.delete(subscriber)) {
                return;
            }
            subscriber.reject(new AutomaticRefreshDeferred());
            if (!entry.subscribers.size) {
                this.forget(entry);
                this.abortTransport(entry, options);
            }
        };
        return promise;
    }

    forget(entry) {
        entry.settled = true;
        this.running.delete(entry);
        if (entry.key !== null && this.entries.get(entry.key) === entry) {
            this.entries.delete(entry.key);
        }
    }

    abortTransport(entry, options = []) {
        if (
            !entry.aborted &&
            entry.request &&
            typeof entry.request.abort === "function"
        ) {
            entry.aborted = true;
            try {
                entry.request.abort(...options);
            } catch (_error) {
                // Consumers have already settled even if transport cancellation fails.
            }
        }
    }

    cancelEntry(entry) {
        this.forget(entry);
        for (const subscriber of entry.subscribers) {
            subscriber.reject(new AutomaticRefreshDeferred());
        }
        entry.subscribers.clear();
        this.abortTransport(entry);
    }

    settle(entry, error, value, rejected = false) {
        // Auth/company changes must invalidate before admitting any old result.
        this.checkContext();
        if (entry.settled || entry.epoch !== this.epoch) {
            return;
        }
        let encoded = null;
        let failure = error;
        let failed = rejected;
        if (!failed) {
            try {
                encoded = canonicalSharedReadJson(value);
            } catch (invalidResult) {
                failure = invalidResult;
                failed = true;
            }
        }
        this.forget(entry);
        for (const subscriber of entry.subscribers) {
            if (failed) {
                subscriber.reject(failure);
            } else {
                subscriber.resolve(JSON.parse(encoded));
            }
        }
        entry.subscribers.clear();
    }

    destroy() {
        this.destroyed = true;
        for (const entry of [...this.running]) {
            this.cancelEntry(entry);
        }
    }
}

export const sharedReadsService = {
    dependencies: ["orm", "user", "bus_service"],
    start(env, {orm, user, bus_service: busService}) {
        const pool = new SharedReadPool({
            orm: orm.silent || orm,
            getContext: () => {
                const context = user.context || {};
                const company = env.services.company;
                const currentCompany = (company && company.currentCompany) || {};
                const companies =
                    context.allowed_company_ids ||
                    (company && company.allowedCompanyIds) ||
                    [];
                return {
                    userId: user.userId || context.uid || null,
                    companyId:
                        currentCompany.id || context.company_id || companies[0] || null,
                    activeCompanyIds: companies,
                    lang: user.lang || context.lang || null,
                    tz: user.tz || context.tz || null,
                    rpcContext: context,
                };
            },
        });
        const onNotification = ({detail}) => {
            if (
                Array.isArray(detail) &&
                detail.some(
                    (notification) =>
                        notification &&
                        notification.type === CONTACT_CENTER_NOTIFICATION_TYPE &&
                        notification.payload &&
                        notification.payload.schema_version ===
                            SUPPORTED_SCHEMA_VERSION &&
                        notification.payload.event_type === CONNECTION_HEALTH_EVENT_TYPE
                )
            ) {
                pool.invalidate();
            }
        };
        const onReconnect = () => pool.invalidate();
        // Services start before stores subscribe, so the causal barrier runs first.
        busService.addEventListener("notification", onNotification);
        busService.addEventListener("reconnect", onReconnect);
        return {
            call: pool.call.bind(pool),
            invalidate: pool.invalidate.bind(pool),
            checkContext: pool.checkContext.bind(pool),
            destroy() {
                busService.removeEventListener("notification", onNotification);
                busService.removeEventListener("reconnect", onReconnect);
                pool.destroy();
            },
        };
    },
};

registry.category("services").add("contact_center_ui.shared_reads", sharedReadsService);
