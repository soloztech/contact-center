/** @odoo-module **/
import {
    ContactCenterStore,
    conversationListRow,
} from "@contact_center_ui/js/contact_center_store.esm";

export function e2Deferred() {
    let resolve = false,
        reject = false;
    const promise = new Promise((ok, fail) => {
        resolve = ok;
        reject = fail;
    });
    promise.abort = () => reject(new Error("aborted"));
    return {promise, resolve, reject};
}
export async function e2Settle() {
    for (let i = 0; i < 40; i++) {
        await Promise.resolve();
    }
}
export function e2Clock() {
    const tasks = new Map();
    let next = 0,
        now = 0;
    return {
        tasks,
        now: () => now,
        setTimeout(callback, delay) {
            const id = ++next;
            tasks.set(id, {callback, at: now + delay});
            return id;
        },
        clearTimeout(id) {
            tasks.delete(id);
        },
        async advance(ms) {
            const until = now + ms;
            for (;;) {
                const task = [...tasks]
                    .filter(([, value]) => value.at <= until)
                    .sort((a, b) => a[1].at - b[1].at)[0];
                if (!task) {
                    break;
                }
                tasks.delete(task[0]);
                now = task[1].at;
                task[1].callback();
                await e2Settle();
            }
            now = until;
            await e2Settle();
        },
    };
}
export function e2Full(channelId = 10, values = {}) {
    return {
        channel_id: channelId,
        conversation_type: "direct",
        name: `Customer ${channelId}`,
        state: "open",
        ignored: false,
        unread_count: 2,
        first_unread_message_id: 3,
        last_activity_at: "2026-10-09 12:00:00",
        platform: "whatsapp",
        provider: "wuzapi",
        account: {
            id: 1,
            name: "Inbox",
            platform: "whatsapp",
            can_start_conversation: true,
        },
        identity: {
            id: channelId + 100,
            name: `Customer ${channelId}`,
            avatar_url: false,
            partner: {
                id: 50,
                name: "Partner",
                is_company: false,
                company: {id: 60, name: "Company"},
            },
            aliases: [{address: "phone"}],
            suggested_phone: "+551100000000",
        },
        group: false,
        responsible: false,
        tags: [],
        last_message: {
            message_id: 4,
            body_text: "Hello",
            direction: "inbound",
            content_type: "text",
            delivery_state: false,
            dispatch_state: false,
            is_deleted: false,
            author: {id: 110, type: "guest", name: "Customer", is_current_user: false},
            media: [],
        },
        preference: {pinned: false, pinned_at: false, muted: false, revision: 0},
        capabilities: {
            delete_conversation: true,
            ignore_conversation: true,
            view_attribution: true,
            send_text: true,
        },
        can_send: true,
        retention: {preserve: true},
        ...values,
    };
}
export function e2Store({items = [e2Full()], respond = false} = {}) {
    const timer = e2Clock(),
        calls = [],
        user = {
            userId: 7,
            context: {allowed_company_ids: [1], lang: "en_US", tz: "UTC"},
        };
    const server = {items, respond};
    const orm = {
        user,
        call(model, method, args = [], kwargs = {}) {
            calls.push({model, method, args, kwargs});
            if (server.respond) {
                const value = server.respond(method, args, kwargs);
                if (value !== undefined) {
                    return value;
                }
            }
            if (method === "get_conversation") {
                return Promise.resolve({
                    schema_version: 1,
                    item: server.items.find((row) => row.channel_id === args[0]),
                });
            }
            if (method === "get_timeline") {
                return Promise.resolve({
                    schema_version: 1,
                    channel_id: args[0],
                    items: [],
                    has_more: false,
                    next_before_message_id: false,
                });
            }
            if (method === "list_conversations") {
                return Promise.resolve({
                    schema_version: 1,
                    projection: "list_v1",
                    items: server.items.map(conversationListRow),
                    total: server.items.length,
                    has_more: false,
                    next_cursor: false,
                });
            }
            if (method === "get_connection_health") {
                return Promise.resolve({
                    schema_version: 1,
                    items: [],
                    summary: {total: 0},
                });
            }
            throw new Error(`Unexpected method ${method}`);
        },
    };
    const visible = new EventTarget();
    visible.hidden = false;
    const store = new ContactCenterStore({
        orm,
        busService: new EventTarget(),
        notification: false,
        healthTimer: timer,
        realtimeTimer: timer,
        contactTimer: timer,
        companyTimer: timer,
        inboxPreferenceTimer: timer,
        refreshNow: timer.now,
        refreshRandom: () => 0,
        visibilityDocument: visible,
        focusTarget: new EventTarget(),
        inboxPreferenceStorage: false,
        inboxDensityStorage: false,
        operationStorage: false,
    });
    store.state.bootstrap = {
        user: {id: 7},
        capabilities: {link_contact: true, link_company: true, manage_assignment: true},
        accounts: [{id: 1, can_start_conversation: true}],
    };
    store.ensureDetailContext();
    store.state.conversations = items.map(conversationListRow);
    store.state.listPhase = "ready";
    store.listWindowFilterRevision = store.filterRevision;
    return {store, timer, calls, server, user};
}
export function e2Metadata(channelId = 10, scope = "identity_name") {
    return {
        schema_version: 1,
        event_type: "conversation_updated",
        channel_id: channelId,
        update_scope_version: 1,
        update_scope: scope,
        changed_fields: [scope],
    };
}
