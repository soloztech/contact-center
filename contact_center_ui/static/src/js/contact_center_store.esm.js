/** @odoo-module **/

import {
    CONNECTION_HEALTH_EVENT_TYPE,
    compareTimelineItems,
    contactCenterNotifications,
    conversationPreference,
    conversationStateMeta,
    conversationUiPolicy,
    conversationUpdateScope,
    filterConversationsByResponsibility,
    isRenderableConversation,
    isResponsibilityScope,
    makeClientRequestId,
    mergeConnectionHealth,
    mergeTimelineItems,
    messageActionEnabled,
    normalizeAttributionProjection,
    normalizeConnectionHealth,
    normalizeConversationGroup,
    normalizePartnerCompany,
    partnerCompanyForIdentity,
    secondaryCompaniesForIdentity,
    validateEnvelope,
} from "./contact_center_model.esm";
import {
    AutomaticReadOwner,
    AutomaticRefreshDeferred,
    CoalescedRefresh,
} from "./contact_center_refresh.esm";
import {_t} from "@web/core/l10n/translation";
import {browser} from "@web/core/browser/browser";
import {outboundStructuredCapabilities} from "./structured_content.esm";
import {registry} from "@web/core/registry";
import {session} from "@web/session";

const API_MODEL = "contact.center.ui.api";
const LIST_LIMIT = 50;
const REALTIME_REFRESH_LIMIT = 200;
// Conversations one "Marcar todas como lidas" action reads at most (L08).
const BULK_READ_LIMIT = 200;
const TIMELINE_LIMIT = 100;
const TIMELINE_REFRESH_LIMIT = 100;
const TIMELINE_FORWARD_MAX_PAGES = 20;
const TIMELINE_MODES = new Set(["reset", "older", "newer", "refresh_latest"]);
const SEEN_RETRY_DELAYS = Object.freeze([1000, 3000, 10000]);
const CONNECTION_HEALTH_INVALIDATION_DELAY = 160;
const INBOX_DENSITY_STORAGE_KEY = "contact_center_ui.inbox_density.v1";
const INBOX_DENSITIES = new Set(["comfortable", "compact"]);
const INBOX_STATE_PREFIX = "contact_center_ui.inbox_state";
const INBOX_STATE_VERSION = 1;
const INBOX_STATE_SAVE_DELAY = 400;
const INBOX_LIST_VIEWS = new Set(["grouped", "flat"]);
const INBOX_SIDE_PANELS = new Set(["contact", "crm"]);
const ACTIVITY_TIMINGS = new Set(["all", "due", "overdue", "today", "planned"]);
const CONVERSATION_STATE_KEYS = Object.freeze(["open", "resolved", "archived"]);
const CONVERSATION_STATES = new Set(CONVERSATION_STATE_KEYS);
// The bus is an invalidation transport, not the source of truth.  Reconcile at
// a low frequency even while connected so that a notification lost during a
// browser sleep, SharedWorker hand-off, or reconnect cannot leave the inbox
// stale indefinitely.
const CONSISTENCY_SYNCHRONIZATION_INTERVAL = 30 * 1000;
const CONNECTION_HEALTH_REFRESH_DELAYS = Object.freeze([
    500, 1000, 2000, 4000, 8000, 15000, 30000, 30000, 30000, 30000, 30000,
]);
const SYNCHRONIZING_EVENTS = new Set([
    "conversation_updated",
    "conversation_preference_updated",
    "delivery_updated",
    "identity_updated",
    "message_created",
    "message_updated",
    "message_deleted",
    "reaction_updated",
    "media_updated",
    "productivity_updated",
]);
// Events after which a conversation snapshot requested earlier may be stale in
// its content or read state (L08-I02, I04). The member_seen of this user's own
// reads is counted apart: it can only lower an unread count (L08-I06).
const SNAPSHOT_INVALIDATING_EVENTS = new Set([
    "conversation_updated",
    "identity_updated",
    "media_updated",
    "message_created",
    "message_deleted",
    "message_updated",
    "reaction_updated",
]);
const MESSAGE_ACTION_METHODS = Object.freeze({
    react_message: "react",
    edit_message: "edit",
    delete_message: "delete",
    resend_message: "resend",
});
const IDENTITY_LINK_TARGETS = new Set(["person", "central_company"]);
const QUICK_REPLY_LIMIT = 20;
const OPERATION_JOURNAL_VERSION = 1;
const OPERATION_JOURNAL_PREFIX = "contact_center_ui.operation_journal";
const OPERATION_JOURNAL_MAX_ENTRIES = 200;
const OPERATION_JOURNAL_TTL_MS = 24 * 60 * 60 * 1000;
const OPERATION_FINGERPRINT_PATTERN = /^[0-9a-f]{32}$/;
const UUID_PATTERN =
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function normalizeConversationStateFilters(value) {
    if (
        !Array.isArray(value) ||
        value.some((state) => !CONVERSATION_STATES.has(state))
    ) {
        return false;
    }
    const selected = new Set(value);
    return CONVERSATION_STATE_KEYS.filter((state) => selected.has(state));
}

function positiveFilterIds(value) {
    return [...new Set((Array.isArray(value) ? value : []).map(Number))]
        .filter((id) => Number.isSafeInteger(id) && id > 0)
        .slice(0, 50);
}

function timelineLoadingPhase(mode) {
    if (mode === "reset") {
        return "loading";
    }
    return mode === "newer" ? "loading_newer" : "loading_more";
}

function initialUnreadMessageId(mode, conversation) {
    const messageId = conversation && conversation.first_unread_message_id;
    return mode === "reset" && Number.isSafeInteger(messageId) && messageId > 0
        ? messageId
        : false;
}

function timelineQuery(mode, limit, beforeMessageId, afterMessageId, anchorMessageId) {
    const query = {
        before_message_id: mode === "older" ? beforeMessageId : false,
        limit,
    };
    if (mode === "newer") {
        query.after_chronological_message_id = afterMessageId;
    }
    if (anchorMessageId) {
        query.anchor_message_id = anchorMessageId;
    }
    return query;
}

function isPlainRecord(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function normalizeCatalogTag(value) {
    if (
        !isPlainRecord(value) ||
        !Number.isSafeInteger(value.id) ||
        value.id <= 0 ||
        typeof value.name !== "string" ||
        !value.name.trim() ||
        !Number.isSafeInteger(value.color)
    ) {
        return false;
    }
    return {id: value.id, name: value.name, color: value.color};
}

function compareCatalogTags(left, right) {
    if (left.name === right.name) {
        return left.id - right.id;
    }
    return left.name < right.name ? -1 : 1;
}

function browserSessionStorage() {
    try {
        return window.sessionStorage;
    } catch (_error) {
        return false;
    }
}

export function operationJournalStorageKey(database, userId) {
    const cleanDatabase =
        typeof database === "string" ? database.trim().slice(0, 160) : "";
    const cleanUserId = Number(userId);
    if (!cleanDatabase || !Number.isSafeInteger(cleanUserId) || cleanUserId <= 0) {
        return false;
    }
    return `${OPERATION_JOURNAL_PREFIX}.v${OPERATION_JOURNAL_VERSION}.${encodeURIComponent(
        cleanDatabase
    )}.${cleanUserId}`;
}

/**
 * Return a stable opaque identifier for an operation intent.
 *
 * The journal recognizes an exact retry without persisting cleartext message
 * bodies, notes, phone numbers or other customer data in this journal.  This is
 * an idempotency fingerprint, not a cryptographic confidentiality boundary; a
 * hash collision remains fail-safe because the server rejects UUID reuse with
 * a different canonical payload.
 *
 * @param {String} signature canonical in-memory intent signature
 * @returns {String} opaque 128-bit hexadecimal fingerprint
 */
export function operationIntentFingerprint(signature) {
    const value = typeof signature === "string" ? signature : "";
    const lanes = [0x811c9dc5, 0x9e3779b9, 0x85ebca6b, 0xc2b2ae35];
    const multipliers = [0x01000193, 0x27d4eb2d, 0x165667b1, 0x9e3779b1];
    for (let index = 0; index < value.length; index += 1) {
        const code = value.charCodeAt(index);
        for (let lane = 0; lane < lanes.length; lane += 1) {
            lanes[lane] = Math.imul(
                lanes[lane] ^ (code + index + lane * 131),
                multipliers[lane]
            );
            lanes[lane] ^= lanes[lane] >>> 16;
        }
    }
    return lanes.map((lane) => (lane >>> 0).toString(16).padStart(8, "0")).join("");
}

function sanitizedOperationJournalEntry(value, now) {
    if (!isPlainRecord(value)) {
        return false;
    }
    const fingerprint = typeof value.fingerprint === "string" ? value.fingerprint : "";
    const requestId =
        typeof value.request_id === "string"
            ? value.request_id.trim().toLowerCase()
            : "";
    const updatedAt = Number(value.updated_at);
    if (
        !OPERATION_FINGERPRINT_PATTERN.test(fingerprint) ||
        !UUID_PATTERN.test(requestId) ||
        !Number.isSafeInteger(updatedAt) ||
        updatedAt <= 0 ||
        updatedAt > now + 5 * 60 * 1000 ||
        now - updatedAt > OPERATION_JOURNAL_TTL_MS
    ) {
        return false;
    }
    return {fingerprint, requestId, updatedAt};
}

export function readOperationJournal(storage, key, now = Date.now()) {
    const entries = new Map();
    if (!storage || !key) {
        return entries;
    }
    try {
        const parsed = JSON.parse(storage.getItem(key) || "{}");
        const records =
            parsed &&
            parsed.version === OPERATION_JOURNAL_VERSION &&
            Array.isArray(parsed.entries)
                ? parsed.entries.slice(-OPERATION_JOURNAL_MAX_ENTRIES)
                : [];
        for (const record of records) {
            const entry = sanitizedOperationJournalEntry(record, now);
            if (entry) {
                entries.delete(entry.fingerprint);
                entries.set(entry.fingerprint, entry);
            }
        }
    } catch (_error) {
        return new Map();
    }
    return entries;
}

export function writeOperationJournal(storage, key, entries, now = Date.now()) {
    if (!storage || !key || !(entries instanceof Map)) {
        return false;
    }
    const records = [...entries.values()]
        .map((entry) =>
            sanitizedOperationJournalEntry(
                {
                    fingerprint: entry.fingerprint,
                    request_id: entry.requestId,
                    updated_at: entry.updatedAt,
                },
                now
            )
        )
        .filter(Boolean)
        .sort((left, right) => left.updatedAt - right.updatedAt)
        .slice(-OPERATION_JOURNAL_MAX_ENTRIES)
        .map((entry) => ({
            fingerprint: entry.fingerprint,
            request_id: entry.requestId,
            updated_at: entry.updatedAt,
        }));
    try {
        if (!records.length) {
            storage.removeItem(key);
            return true;
        }
        storage.setItem(
            key,
            JSON.stringify({version: OPERATION_JOURNAL_VERSION, entries: records})
        );
        return true;
    } catch (_error) {
        return false;
    }
}

export function validateOperationEnvelope(
    payload,
    {channelId, requestId, requiredRecords = []}
) {
    validateEnvelope(payload);
    const responseChannelId = Number(payload.channel_id);
    if (
        !Number.isSafeInteger(responseChannelId) ||
        responseChannelId <= 0 ||
        responseChannelId !== channelId
    ) {
        throw new TypeError("A resposta pertence a outra conversa.");
    }
    if (payload.client_request_id !== requestId) {
        throw new TypeError("A resposta pertence a outra operação.");
    }
    for (const fieldName of requiredRecords) {
        if (!isPlainRecord(payload[fieldName])) {
            throw new TypeError("O servidor não confirmou a operação.");
        }
    }
    return payload;
}

function productivityProjection(channelId = false) {
    return {
        channelId,
        phase: "idle",
        activities: [],
        scheduledMessages: [],
        activityTypes: [],
        assignableUsers: [],
        error: "",
    };
}

function productivityItems(payload, fieldName) {
    const items = payload && payload[fieldName];
    if (!Array.isArray(items) || !items.every(isPlainRecord)) {
        throw new TypeError("A produtividade retornada pelo servidor é inválida.");
    }
    return items;
}

export function normalizeProductivity(payload, channelId) {
    if (!isPlainRecord(payload)) {
        throw new TypeError("A produtividade retornada pelo servidor é inválida.");
    }
    const requestedChannelId = Number(channelId);
    if (!Number.isSafeInteger(requestedChannelId) || requestedChannelId <= 0) {
        throw new TypeError("A conversa solicitada para produtividade é inválida.");
    }
    const responseChannelId = Number(payload.channel_id);
    if (
        !Number.isSafeInteger(responseChannelId) ||
        responseChannelId <= 0 ||
        responseChannelId !== requestedChannelId
    ) {
        throw new TypeError("A produtividade retornada pertence a outra conversa.");
    }
    return {
        channelId: responseChannelId,
        phase: "ready",
        activities: productivityItems(payload, "activities"),
        scheduledMessages: productivityItems(payload, "scheduled_messages"),
        activityTypes: productivityItems(payload, "activity_types"),
        assignableUsers: productivityItems(payload, "assignable_users"),
        error: "",
    };
}

export function normalizeQuickReplies(payload, channelId) {
    if (!isPlainRecord(payload)) {
        throw new TypeError("As respostas rápidas retornadas são inválidas.");
    }
    const requestedChannelId = Number(channelId);
    const responseChannelId = Number(payload.channel_id);
    if (
        !Number.isSafeInteger(requestedChannelId) ||
        requestedChannelId <= 0 ||
        !Number.isSafeInteger(responseChannelId) ||
        responseChannelId <= 0 ||
        responseChannelId !== requestedChannelId
    ) {
        throw new TypeError(
            "As respostas rápidas retornadas pertencem a outra conversa."
        );
    }
    if (
        !Array.isArray(payload.items) ||
        payload.items.length > QUICK_REPLY_LIMIT ||
        !payload.items.every(isPlainRecord)
    ) {
        throw new TypeError("As respostas rápidas retornadas são inválidas.");
    }
    return payload.items.map((item) => {
        const id = Number(item.id);
        const bindingId = Number(item.binding_id);
        const scope = typeof item.scope === "string" ? item.scope.trim() : "";
        const shortcut = typeof item.shortcut === "string" ? item.shortcut.trim() : "";
        const body = typeof item.body === "string" ? item.body : "";
        if (
            !Number.isSafeInteger(id) ||
            id <= 0 ||
            !Number.isSafeInteger(bindingId) ||
            bindingId <= 0 ||
            !["company", "team", "account", "channel"].includes(scope) ||
            !shortcut ||
            !body.trim()
        ) {
            throw new TypeError("As respostas rápidas retornadas são inválidas.");
        }
        return {
            id,
            bindingId,
            scope,
            shortcut,
            name: shortcut,
            body,
            description: typeof item.description === "string" ? item.description : "",
        };
    });
}

function validateRetentionPreview(policy, impact) {
    if (
        !policy.can_manage ||
        !isPlainRecord(impact) ||
        !Number.isSafeInteger(impact.message_count) ||
        impact.message_count < 0 ||
        !Number.isSafeInteger(impact.media_count) ||
        impact.media_count < 0 ||
        typeof impact.confirmation_token !== "string" ||
        !impact.confirmation_token ||
        typeof impact.cutoff !== "string" ||
        !impact.cutoff
    ) {
        throw new TypeError("A simulação de retenção retornada é inválida.");
    }
}

export function validateRetentionResponse(payload, channelId, preview = false) {
    validateEnvelope(payload);
    const policy = payload && payload.policy;
    if (
        payload.channel_id !== channelId ||
        !isPlainRecord(policy) ||
        ["supported", "enabled", "effective", "preserve", "can_manage"].some(
            (name) => typeof policy[name] !== "boolean"
        ) ||
        !Number.isSafeInteger(policy.days) ||
        policy.days <= 0 ||
        !Number.isSafeInteger(policy.revision) ||
        policy.revision < 0 ||
        policy.effective !== (policy.supported && policy.enabled && !policy.preserve)
    ) {
        throw new TypeError("A política de retenção retornada é inválida.");
    }
    if (preview) {
        validateRetentionPreview(policy, payload.preview);
    }
    return payload;
}

export function formatOdooUtcDateTime(value) {
    const date = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(date.getTime())) {
        throw new TypeError("A data e hora informadas são inválidas.");
    }
    return date.toISOString();
}

export function localDateTimeToOdooUtc(value) {
    const match =
        typeof value === "string" &&
        value.trim().match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/);
    if (!match) {
        throw new TypeError("A data e hora informadas são inválidas.");
    }
    const parts = match.slice(1).map((part) => Number(part || 0));
    const date = new Date(
        parts[0],
        parts[1] - 1,
        parts[2],
        parts[3],
        parts[4],
        parts[5],
        0
    );
    if (
        date.getFullYear() !== parts[0] ||
        date.getMonth() !== parts[1] - 1 ||
        date.getDate() !== parts[2] ||
        date.getHours() !== parts[3] ||
        date.getMinutes() !== parts[4] ||
        date.getSeconds() !== parts[5]
    ) {
        throw new TypeError("A data e hora informadas são inválidas.");
    }
    return formatOdooUtcDateTime(date);
}

function browserLocalStorage() {
    try {
        return window.localStorage;
    } catch (_error) {
        return false;
    }
}

export function loadInboxDensityPreference(storage = browserLocalStorage()) {
    try {
        const value = storage && storage.getItem(INBOX_DENSITY_STORAGE_KEY);
        return INBOX_DENSITIES.has(value) ? value : "comfortable";
    } catch (_error) {
        return "comfortable";
    }
}

export function saveInboxDensityPreference(density, storage = browserLocalStorage()) {
    if (!INBOX_DENSITIES.has(density)) {
        return false;
    }
    try {
        if (!storage) {
            return false;
        }
        storage.setItem(INBOX_DENSITY_STORAGE_KEY, density);
        return true;
    } catch (_error) {
        return false;
    }
}

export function inboxStateStorageKey(database, userId) {
    const cleanDatabase =
        typeof database === "string" ? database.trim().slice(0, 160) : "";
    const cleanUserId = Number(userId);
    if (!cleanDatabase || !Number.isSafeInteger(cleanUserId) || cleanUserId <= 0) {
        return false;
    }
    return `${INBOX_STATE_PREFIX}.v${INBOX_STATE_VERSION}.${encodeURIComponent(
        cleanDatabase
    )}.${cleanUserId}`;
}

function catalogIds(items) {
    return new Set(
        (Array.isArray(items) ? items : [])
            .map((item) => isPlainRecord(item) && item.id)
            .filter((id) => Number.isSafeInteger(id) && id > 0)
    );
}

const FILTER_PREFERENCE_RULES = Object.freeze({
    accountId: (value, catalog) => catalog.accounts.has(value),
    responsibility: (value) => isResponsibilityScope(value),
    responsibleId: (value, catalog) => catalog.agents.has(value),
    unreadOnly: (value) => typeof value === "boolean",
    excludeMuted: (value) => typeof value === "boolean",
    conversationType: (value) => ["direct", "group"].includes(value),
    activityTiming: (value) => ACTIVITY_TIMINGS.has(value),
});
const LAYOUT_PREFERENCE_RULES = Object.freeze({
    detailsOpen: (value) => typeof value === "boolean",
    listView: (value) => INBOX_LIST_VIEWS.has(value),
    sidePanel: (value) => INBOX_SIDE_PANELS.has(value),
});

function acceptedPreferences(values, rules, catalog) {
    const result = {};
    for (const [key, accepts] of Object.entries(rules)) {
        if (accepts(values[key], catalog)) {
            result[key] = values[key];
        }
    }
    return result;
}

function sanitizedFilterPreferences(filters, catalog) {
    const result = acceptedPreferences(filters, FILTER_PREFERENCE_RULES, catalog);
    if (result.responsibleId) {
        // Same rule as the filter itself: a named responsible replaces a scope.
        result.responsibility = "all";
    }
    const states = normalizeConversationStateFilters(filters.states);
    if (states !== false) {
        result.states = states;
    }
    if (Array.isArray(filters.tagIds)) {
        result.tagIds = positiveFilterIds(filters.tagIds).filter((id) =>
            catalog.tags.has(id)
        );
    }
    return result;
}

function sanitizedLayoutPreferences(layout, catalog) {
    const result = acceptedPreferences(layout, LAYOUT_PREFERENCE_RULES, catalog);
    if (isPlainRecord(layout.collapsedInboxes)) {
        result.collapsedInboxes = Object.fromEntries(
            Object.entries(layout.collapsedInboxes)
                .filter(
                    ([key, flag]) =>
                        flag === true &&
                        (key === "inbox:unknown" ||
                            (key.startsWith("inbox:") &&
                                catalog.accounts.has(Number(key.slice(6)))))
                )
                .map(([key]) => [key, true])
        );
    }
    return result;
}

/**
 * Keep only saved inbox preferences that still make sense for this bootstrap.
 *
 * Preferences are a convenience: anything unknown, stale or malformed is
 * dropped silently and the default applies instead.
 *
 * @param {*} value parsed storage payload
 * @param {Object} bootstrap validated bootstrap payload
 * @returns {Object} sanitized {filters, layout}; empty objects when nothing applies
 */
export function sanitizeInboxPreferences(value, bootstrap) {
    if (!isPlainRecord(value) || value.version !== INBOX_STATE_VERSION) {
        return {filters: {}, layout: {}};
    }
    const source = isPlainRecord(bootstrap) ? bootstrap : {};
    const catalog = {
        accounts: catalogIds(source.accounts),
        agents: catalogIds(source.agents),
        tags: catalogIds(source.tags),
    };
    return {
        filters: sanitizedFilterPreferences(
            isPlainRecord(value.filters) ? value.filters : {},
            catalog
        ),
        layout: sanitizedLayoutPreferences(
            isPlainRecord(value.layout) ? value.layout : {},
            catalog
        ),
    };
}

export function readInboxPreferences(storage, key, bootstrap) {
    try {
        const raw = storage && key ? storage.getItem(key) : null;
        return sanitizeInboxPreferences(raw ? JSON.parse(raw) : false, bootstrap);
    } catch (_error) {
        return sanitizeInboxPreferences(false, bootstrap);
    }
}

export function writeInboxPreferences(storage, key, filters, layout) {
    if (!storage || !key) {
        return false;
    }
    try {
        storage.setItem(
            key,
            JSON.stringify({version: INBOX_STATE_VERSION, filters, layout})
        );
        return true;
    } catch (_error) {
        return false;
    }
}

/**
 * Per-document memory of where the agent left the inbox.
 *
 * It lives in a web client service, so it survives leaving and re-entering the
 * inbox action but never a reload, a new tab or a duplicated tab.  Unlike
 * sessionStorage, browsers never copy it into another tab.
 *
 * @returns {Object} mutable context owned by the current document
 */
export function createInboxDocumentContext() {
    return {
        userId: false,
        channelId: false,
        query: "",
        listScrollTop: 0,
        loadedCount: 0,
        // The inbox instance that may write this memory: the newest one.
        owner: false,
    };
}

/**
 * Action parameters apply to the first mount of their action only.
 *
 * A breadcrumb return reuses the same action object; it must follow the
 * remembered state of this document instead of the original directed opening.
 *
 * @param {Object} context document context service
 * @param {Object} action client action descriptor
 * @returns {Object|Boolean} parameters to apply, or false
 */
export function consumeInboxActionParams(context, action) {
    if (!action || !action.params) {
        return false;
    }
    if (!context) {
        return action.params;
    }
    context.consumedActions = context.consumedActions || new WeakSet();
    if (context.consumedActions.has(action)) {
        return false;
    }
    context.consumedActions.add(action);
    return action.params;
}

registry.category("services").add("contact_center_ui.inbox_context", {
    start() {
        return createInboxDocumentContext();
    },
});

function uploadErrorMessage(payload) {
    const error = payload && payload.error;
    return (
        (payload && typeof payload.message === "string" && payload.message) ||
        (typeof error === "string" && error) ||
        (error && typeof error.message === "string" && error.message) ||
        (error &&
            error.data &&
            typeof error.data.message === "string" &&
            error.data.message) ||
        "O arquivo não pôde ser enviado."
    );
}

function appendAudioUploadMetadata(formData, isVoiceNote, durationSeconds) {
    if (durationSeconds) {
        if (!Number.isSafeInteger(durationSeconds) || durationSeconds <= 0) {
            throw new Error("A duração da gravação de áudio é inválida.");
        }
        formData.append("duration_seconds", String(durationSeconds));
    }
    if (!isVoiceNote) {
        return;
    }
    if (!durationSeconds) {
        throw new Error("A duração da mensagem de voz é inválida.");
    }
    formData.append("is_voice_note", "1");
}

async function uploadResponsePayload(response) {
    if (response.status === 401 || response.status === 403) {
        throw new Error(
            "Sua sessão não permite enviar este arquivo. Atualize a página e tente novamente."
        );
    }
    let payload = null;
    try {
        payload = await response.json();
    } catch (_error) {
        throw new Error("O servidor não retornou uma resposta válida para o arquivo.");
    }
    if (!response.ok || (payload && payload.error)) {
        throw new Error(uploadErrorMessage(payload));
    }
    validateEnvelope(payload);
    if (typeof payload.media_ref !== "string" || !payload.media_ref.trim()) {
        throw new Error("O servidor não retornou a referência do arquivo.");
    }
    return payload;
}

export async function uploadMediaRequest({
    channelId,
    file,
    clientUploadId,
    isVoiceNote = false,
    durationSeconds = 0,
    fetcher = window.fetch.bind(window),
    formDataFactory = () => new window.FormData(),
    csrfToken = (window.odoo && window.odoo.csrf_token) || "",
    signal = undefined,
}) {
    const formData = formDataFactory();
    formData.append("channel_id", String(channelId));
    formData.append("client_upload_id", clientUploadId);
    formData.append("file", file, file.name);
    appendAudioUploadMetadata(formData, isVoiceNote, durationSeconds);
    if (csrfToken) {
        formData.append("csrf_token", csrfToken);
    }
    const response = await fetcher("/contact_center/media/upload", {
        method: "POST",
        body: formData,
        credentials: "same-origin",
        headers: {Accept: "application/json"},
        signal,
    });
    return uploadResponsePayload(response);
}

function errorMessage(error) {
    return (
        (error && error.data && (error.data.message || error.data.name)) ||
        (error && error.message) ||
        "A operação não pôde ser concluída."
    );
}

function accessWasRevoked(error) {
    const data = (error && error.data) || {};
    return (
        data.exception_type === "access_error" ||
        data.name === "odoo.exceptions.AccessError" ||
        data.name === "odoo.exceptions.MissingError" ||
        data.name === "odoo.exceptions.ValidationError"
    );
}

function normalizedMediaRefs(mediaRefs) {
    return Array.from(
        new Set(
            (Array.isArray(mediaRefs) ? mediaRefs : [])
                .filter((item) => typeof item === "string")
                .map((item) => item.trim())
                .filter(Boolean)
        )
    ).slice(0, 1);
}

function normalizedConversationItems(items) {
    const result = [];
    const seen = new Set();
    for (const item of Array.isArray(items) ? items : []) {
        const normalized = normalizeConversationGroup(item);
        if (!isRenderableConversation(normalized) || seen.has(normalized.channel_id)) {
            continue;
        }
        seen.add(normalized.channel_id);
        result.push(normalized);
    }
    return result;
}

function cursorText(value) {
    return typeof value === "string" ? value.trim() : "";
}

function normalizedConversationCursor(item, cursor) {
    const channelId = Number(item && item.channel_id);
    const cursorChannelId = Number(cursor && cursor.channel_id);
    const segment = cursor && cursor.segment;
    if (
        !Number.isSafeInteger(channelId) ||
        channelId <= 0 ||
        !Number.isSafeInteger(cursorChannelId) ||
        cursorChannelId <= 0 ||
        !["pinned", "activity"].includes(segment)
    ) {
        return false;
    }
    return {
        channelId,
        cursorChannelId,
        segment,
        pinnedAt: cursorText(item && item.preference && item.preference.pinned_at),
        cursorPinnedAt: cursorText(cursor && cursor.pinned_at),
        activityAt: cursorText(item && item.last_activity_at),
        cursorActivityAt: cursorText(cursor && cursor.last_activity_at),
    };
}

function orderedValueFollows(value, itemId, cursorValue, cursorId) {
    return Boolean(
        value &&
            cursorValue &&
            (value < cursorValue || (value === cursorValue && itemId < cursorId))
    );
}

export function conversationFollowsCursor(item, cursor) {
    const boundary = normalizedConversationCursor(item, cursor);
    if (!boundary) {
        return false;
    }
    const preference = conversationPreference(item);
    if (boundary.segment === "pinned") {
        if (!preference.pinned) {
            // Every activity-sorted row follows the complete pinned segment.
            return true;
        }
        return orderedValueFollows(
            boundary.pinnedAt,
            boundary.channelId,
            boundary.cursorPinnedAt,
            boundary.cursorChannelId
        );
    }
    if (preference.pinned) {
        return false;
    }
    return orderedValueFollows(
        boundary.activityAt,
        boundary.channelId,
        boundary.cursorActivityAt,
        boundary.cursorChannelId
    );
}

function realtimeConversationPage({
    items,
    hasMore,
    nextCursor,
    total,
    cachedWindow,
    cachedHasMore,
    cachedNextCursor,
    targetCount,
}) {
    const refreshStoppedAtSafeLimit =
        cachedWindow.length > targetCount && items.length >= targetCount && hasMore;
    if (!refreshStoppedAtSafeLimit) {
        return {items, hasMore, nextCursor};
    }
    const freshWindow = normalizedConversationItems(items);
    const cachedIds = new Set(cachedWindow.map((item) => item.channel_id));
    if (!freshWindow.some((item) => cachedIds.has(item.channel_id))) {
        return {items: freshWindow, hasMore, nextCursor};
    }
    // The server cursor, not the position of an overlapping ID, defines the
    // canonical boundary. An old row may have moved deep into the refreshed
    // prefix after new activity; its former position must not truncate the tail.
    const refreshedIds = new Set(freshWindow.map((item) => item.channel_id));
    const preservedTail = cachedWindow
        .filter((item) => conversationFollowsCursor(item, nextCursor))
        .filter((item) => !refreshedIds.has(item.channel_id));
    const mergedItems = [...freshWindow, ...preservedTail];
    const allConversationsLoaded =
        Number.isSafeInteger(total) && total >= 0 && mergedItems.length >= total;
    return {
        items: mergedItems,
        hasMore: allConversationsLoaded ? false : cachedHasMore || hasMore,
        nextCursor: allConversationsLoaded
            ? false
            : (preservedTail.length && cachedHasMore && cachedNextCursor) || nextCursor,
    };
}

function timelinePagesOverlap(existing, incoming) {
    const existingIds = new Set(
        existing
            .map((item) => item && item.message_id)
            .filter((messageId) => Number.isSafeInteger(messageId) && messageId > 0)
    );
    return incoming.some((item) => item && existingIds.has(item.message_id));
}

function forwardTimelinePage(payload, afterMessageId) {
    if (
        !payload ||
        !Array.isArray(payload.items) ||
        typeof payload.has_more_forward !== "boolean" ||
        (payload.next_after_message_id !== false &&
            (!Number.isSafeInteger(payload.next_after_message_id) ||
                payload.next_after_message_id <= afterMessageId))
    ) {
        throw new TypeError("A paginação incremental de mensagens é inválida.");
    }
    const items = mergeTimelineItems([], payload.items, {prepend: false});
    if (
        items.some((item) => item.message_id <= afterMessageId) ||
        (items.length &&
            payload.next_after_message_id !==
                Math.max(...items.map((item) => item.message_id))) ||
        (!items.length && payload.next_after_message_id !== false) ||
        (payload.has_more_forward && !items.length)
    ) {
        throw new TypeError("O cursor incremental de mensagens é inválido.");
    }
    return {
        items,
        hasMore: payload.has_more_forward,
        nextAfterMessageId: payload.next_after_message_id,
    };
}

function mergeAttributionProjection(projection, previous, mode) {
    if (!projection.enabled || mode === "reset") {
        return {
            enabled: projection.enabled,
            items: projection.items,
            hasMore: projection.has_more,
            nextCursor: projection.next_cursor,
        };
    }
    if (mode === "append") {
        const byRef = new Map(
            [...previous.items, ...projection.items].map((item) => [
                item.public_ref,
                item,
            ])
        );
        return {
            enabled: true,
            items: [...byRef.values()],
            hasMore: projection.has_more,
            nextCursor: projection.next_cursor,
        };
    }
    const byRef = new Map();
    for (const item of projection.items) {
        byRef.set(item.public_ref, item);
    }
    for (const item of previous.items) {
        if (!byRef.has(item.public_ref)) {
            byRef.set(item.public_ref, item);
        }
    }
    return {
        enabled: true,
        items: [...byRef.values()],
        hasMore: previous.hasMore || projection.has_more,
        nextCursor: (previous.hasMore && previous.nextCursor) || projection.next_cursor,
    };
}

function canStartSend(conversation, body, mediaRefs, sending) {
    return Boolean(
        conversationUiPolicy(conversation).allow_send &&
            (body || mediaRefs.length) &&
            !sending
    );
}

function sendOutcome(options, accepted, definitive = false, requestId = false) {
    return options.returnOutcome ? {accepted, definitive, requestId} : accepted;
}

function definitiveSendRejection(error) {
    const name = error && error.data && error.data.name;
    return [
        "odoo.exceptions.UserError",
        "odoo.exceptions.ValidationError",
        "odoo.exceptions.AccessError",
        "odoo.exceptions.MissingError",
    ].includes(name);
}

function replyMatches(replyTo, replyId) {
    return (!replyTo && !replyId) || (replyTo && replyTo.message_id === replyId);
}

function companyMutationIdentity(
    payload,
    partnerId,
    expectedCompanyId = false,
    relationKind = "primary"
) {
    if (!isPlainRecord(payload) || !isPlainRecord(payload.identity)) {
        throw new Error("O servidor não confirmou o vínculo da empresa.");
    }
    const identity = payload.identity;
    const partner = identity.partner;
    if (!isPlainRecord(partner)) {
        throw new Error("O servidor não confirmou o vínculo da empresa.");
    }
    const company = normalizePartnerCompany(payload.company);
    const linkedCompany =
        relationKind === "secondary"
            ? secondaryCompaniesForIdentity(identity).find(
                  (item) => company && item.id === company.id
              )
            : partnerCompanyForIdentity(identity);
    if (!company || !linkedCompany) {
        throw new Error("O servidor não confirmou o vínculo da empresa.");
    }
    if (partner.id !== partnerId || partner.is_company !== false) {
        throw new Error("O servidor não confirmou o vínculo da empresa.");
    }
    if (relationKind === "primary" && partner.company_linking_allowed !== false) {
        throw new Error("O servidor não confirmou o vínculo da empresa.");
    }
    if (company.id !== linkedCompany.id) {
        throw new Error("O servidor não confirmou o vínculo da empresa.");
    }
    if (expectedCompanyId && company.id !== expectedCompanyId) {
        throw new Error("O servidor não confirmou o vínculo da empresa.");
    }
    return identity;
}

function companyRelationshipExists(identity, companyId, relationKind) {
    return relationKind === "secondary"
        ? secondaryCompaniesForIdentity(identity).some(
              (company) => company.id === companyId
          )
        : (partnerCompanyForIdentity(identity) || {}).id === companyId;
}

function companyUnlinkIdentity(payload, partnerId, companyId, relationKind) {
    const identity = payload.identity;
    if (
        !isPlainRecord(identity) ||
        !isPlainRecord(identity.partner) ||
        identity.partner.id !== partnerId ||
        companyRelationshipExists(identity, companyId, relationKind)
    ) {
        throw new Error("O servidor não confirmou o desvínculo da empresa.");
    }
    return identity;
}

function identityMutationIdentity(
    payload,
    expectedLinkKind,
    expectedIsCompany,
    expectedPartnerId = false
) {
    if (!isPlainRecord(payload) || !isPlainRecord(payload.identity)) {
        throw new Error("O servidor não confirmou o vínculo do cadastro.");
    }
    const identity = payload.identity;
    const partner = identity.partner;
    if (
        !isPlainRecord(partner) ||
        !Number.isSafeInteger(partner.id) ||
        partner.id <= 0 ||
        identity.link_kind !== expectedLinkKind ||
        partner.is_company !== expectedIsCompany ||
        (expectedPartnerId && partner.id !== expectedPartnerId)
    ) {
        throw new Error("O servidor não confirmou o vínculo do cadastro.");
    }
    return identity;
}

const INBOX_PRESETS = new Set(["mine_unread", "all_unread"]);
const BOOLEAN_FILTERS = new Set(["unreadOnly", "excludeMuted"]);

export function normalizeInboxActionParams(value) {
    const params = isPlainRecord(value) ? value : {};
    return {
        channelId:
            Number.isSafeInteger(params.channel_id) && params.channel_id > 0
                ? params.channel_id
                : false,
        activityTiming: ["all", "due", "overdue", "today", "planned"].includes(
            params.activity_timing
        )
            ? params.activity_timing
            : false,
        // Unread presets of the top bar: the list opens with the counted
        // conversations, nothing selected.
        preset: INBOX_PRESETS.has(params.preset) ? params.preset : false,
    };
}

function validBulkReadPreparation(payload) {
    return (
        Array.isArray(payload.targets) &&
        payload.targets.length <= BULK_READ_LIMIT &&
        payload.targets.every(
            (target) =>
                isPlainRecord(target) &&
                ["channel_id", "message_id", "max_message_id"].every(
                    (key) => Number.isSafeInteger(target[key]) && target[key] > 0
                )
        )
    );
}

export class ContactCenterStore {
    constructor({
        orm,
        busService,
        notification,
        stateFactory = (value) => value,
        uploadRequest = uploadMediaRequest,
        healthTimer = browser,
        contactTimer = browser,
        companyTimer = browser,
        realtimeTimer = browser,
        refreshNow = () => performance.now(),
        refreshRandom = Math.random,
        visibilityDocument = document,
        focusTarget = browser,
        inboxDensityStorage = browserLocalStorage(),
        inboxPreferenceStorage = false,
        inboxContext = createInboxDocumentContext(),
        inboxPreferenceTimer = browser,
        operationStorage = browserSessionStorage(),
        operationDatabase = false,
        operationNow = () => Date.now(),
        operationCrypto = window.crypto,
        attention = false,
        initialActionParams = false,
    }) {
        this.initialNavigation = normalizeInboxActionParams(initialActionParams);
        this.orm = orm;
        this.busService = busService;
        this.notification = notification;
        this.inboxDensityStorage = inboxDensityStorage;
        this.inboxPreferenceStorage = inboxPreferenceStorage;
        this.inboxContext = inboxContext;
        this.inboxPreferenceTimer = inboxPreferenceTimer;
        this.inboxPreferenceSaveTimer = null;
        // Nothing is written before the saved preferences were read, so an
        // early exit can never replace them with defaults.
        this.inboxPreferencesLoaded = false;
        this.persistFilters = true;
        this.savedFilterPreferences = {};
        this.inboxLayout = {
            listView: "grouped",
            collapsedInboxes: {},
            sidePanel: "contact",
            // Only an explicit choice is saved; a narrow window never rewrites it.
            detailsOpen: undefined,
        };
        this.inboxPreferencesDirty = false;
        this.pendingListScrollTop = 0;
        // This visit's list position, written into the document memory on exit.
        this.listScrollTop = 0;
        // Restoration bookkeeping: only the agent's own actions cancel it.
        this.selectionRevision = 0;
        this.documentRestore = false;
        // Identifies this inbox as the owner of the document memory.
        this.documentOwner = {};
        this.restoreDeniedChannelId = false;
        // The restored selection until its first timeline page applies.
        this.restoringChannelId = false;
        // Filter revision of the last window the server returned; -1 while
        // no request has succeeded yet.
        this.listWindowFilterRevision = -1;
        this.listLoadsInFlight = 0;
        this.listIdleWaiters = [];
        // Bumped synchronously by every filter edit (search included), so an
        // asynchronous restoration can tell that the agent moved on.
        this.filterRevision = 0;
        this.operationStorage = operationStorage;
        this.operationDatabase = operationDatabase;
        this.operationNow = operationNow;
        this.operationCrypto = operationCrypto;
        this.operationJournalKey = false;
        this.operationJournal = new Map();
        this.attention = attention;
        this.state = stateFactory({
            phase: "loading",
            bootstrap: null,
            listPhase: "idle",
            // "Marcar todas como lidas" is running: the list stays busy.
            bulkReadPending: false,
            conversations: [],
            conversationTotal: 0,
            conversationsHaveMore: false,
            nextConversationCursor: false,
            selectedChannelId: false,
            timelinePhase: "idle",
            timelineChannelId: false,
            messages: [],
            timelineHasMore: false,
            nextBeforeMessageId: false,
            timelineHasMoreForward: false,
            nextAfterChronologicalMessageId: false,
            timelineFirstUnreadMessageId: false,
            attribution: {
                channelId: false,
                phase: "idle",
                enabled: false,
                items: [],
                hasMore: false,
                nextCursor: false,
            },
            sendingChannelIds: [],
            uploading: false,
            replyTo: false,
            detailsOpen: window.innerWidth >= 1200,
            retentionFocusRequest: 0,
            inboxDensity: loadInboxDensityPreference(inboxDensityStorage),
            seenPausedChannelId: false,
            listScrollRestoreRequest: 0,
            mobilePane: "list",
            realtime: "connecting",
            attention: attention
                ? attention.snapshot()
                : {
                      available: false,
                      permission: "unsupported",
                      sound_enabled: false,
                      unseen: 0,
                  },
            connectionHealth: false,
            connectionHealthRevision: 0,
            connectionHealthOpen: false,
            connectionHealthPhase: "idle",
            connectionHealthCheckingId: false,
            connectionHealthError: "",
            filters: {
                states: ["open"],
                accountId: false,
                query: "",
                responsibility: "all",
                responsibleId: false,
                unreadOnly: false,
                excludeMuted: false,
                conversationType: false,
                tagId: false,
                tagIds: [],
                activityTiming: false,
            },
            productivity: productivityProjection(),
            quickReplies: {
                channelId: false,
                phase: "idle",
                query: "",
                items: [],
                error: "",
            },
            contactLinker: {
                open: false,
                channelId: false,
                targetKind: "person",
                mode: "search",
                phase: "idle",
                query: "",
                results: [],
                error: "",
            },
            companyLinker: {
                open: false,
                channelId: false,
                partnerId: false,
                mode: "search",
                relationKind: "primary",
                phase: "idle",
                query: "",
                results: [],
                error: "",
            },
            companyOperationsRevision: 0,
        });
        this.listRequest = 0;
        this.startConversationPending = false;
        this.preservedConversationChannelId = false;
        this.timelineRequest = 0;
        this.retentionTimelineRequest = 0;
        this.timelineForwardChannelId = false;
        this.timelineForwardCursor = false;
        this.timelineForwardPageCount = 0;
        this.timelineContiguousChannelId = false;
        this.timelineContiguousCursor = false;
        this.seenRetryTimer = null;
        this.seenRetryToken = 0;
        this.confirmedSeenPointer = false;
        this.pendingSeenRequests = new Map();
        this.suspendedSeenChannels = new Set();
        this.deletedConversationIds = new Set();
        this.attributionRequest = 0;
        this.searchTimer = null;
        this.contactSearchTimer = null;
        this.contactLinkerRequest = 0;
        this.companySearchTimer = null;
        this.companyLinkerRequest = 0;
        this.pendingCompanyOperations = new Set();
        this.conversationSelectionGuard = null;
        this.visibilityDocument = visibilityDocument;
        this.focusTarget = focusTarget;
        this.syncListPending = false;
        this.syncFullRequested = false;
        this.syncFullActive = false;
        this.syncAvatarActive = false;
        this.syncAvatarChannels = new Set();
        this.conversationDetailReadsInFlight = 0;
        this.lastFullSynchronizationStarted = -Infinity;
        this.syncActiveWaiters = [];
        this.syncCycleStopped = false;
        this.syncCycleFailed = false;
        this.syncRefresh = new CoalescedRefresh({
            timer: realtimeTimer,
            now: refreshNow,
            random: refreshRandom,
            canRun: () => !this.visibilityDocument.hidden,
            run: () => this.runSynchronization(),
        });
        this.syncReads = new AutomaticReadOwner({
            timer: realtimeTimer,
            onDeadline: () => {
                this.syncCycleStopped = true;
                this.syncFullRequested = true;
                this.syncRefresh.noteDeadline();
            },
        });
        this.healthRefresh = new CoalescedRefresh({
            timer: healthTimer,
            now: refreshNow,
            random: refreshRandom,
            debounce: CONNECTION_HEALTH_INVALIDATION_DELAY,
            interval: 0,
            run: () => {
                this.healthRequest = this.fetchConnectionHealth();
                return this.healthRequest;
            },
        });
        this.healthReads = new AutomaticReadOwner({
            timer: healthTimer,
            onDeadline: () => this.healthRefresh.noteDeadline(),
        });
        this.healthInvalidationRequestRevision = 0;
        this.healthRequestBusRevision = 0;
        // Resolved once the scheduled synchronization refreshed the list.
        this.syncWaiters = [];
        this.consistencySyncTimer = null;
        this.healthSyncTimer = null;
        this.healthSyncAttempt = 0;
        this.connectionHealthBusRevision = 0;
        this.connectionHealthInvalidationRevision = 0;
        this.syncReconnect = false;
        this.syncTimeline = false;
        // The newest preference received per conversation, ordered only by the
        // server revision (emenda 2): any source, loaded row or not.
        this.preferenceMemory = new Map();
        // Changes seen per conversation (new or edited messages, updates,
        // retention purges): a snapshot requested before one may be stale in
        // content or read state (L08-I02, I04).
        this.conversationGenerations = new Map();
        // Reads by this user seen per conversation (another tab, or the echo of
        // the bulk action itself).
        this.ownReadGenerations = new Map();
        // After an action that changed many conversations the loaded rows beyond
        // the refresh window cannot be kept coherent: the next refresh applied
        // drops them (L08-I05–I07).
        this.listTailStale = false;
        // A bulk action whose outcome is unknown (its call failed once sent):
        // the server may still commit it, and nothing announces that reliably,
        // so until the page is reloaded no refresh keeps the tail (L08-I10, I11).
        this.bulkReadUncertain = false;
        this.productivityRequest = 0;
        this.quickReplyRequest = 0;
        this.activeUploads = 0;
        this.uploadRequest = uploadRequest;
        this.healthTimer = healthTimer;
        this.contactTimer = contactTimer;
        this.companyTimer = companyTimer;
        this.realtimeTimer = realtimeTimer;
        this.started = false;
        this.destroyed = false;
        this.onNotification = this.onNotification.bind(this);
        this.onConnect = this.onConnect.bind(this);
        this.onReconnect = this.onReconnect.bind(this);
        this.onReconnecting = this.onReconnecting.bind(this);
        this.onDisconnect = this.onDisconnect.bind(this);
        this.onPageHide = this.onPageHide.bind(this);
        this.onRefreshVisibility = () => {
            if (this.visibilityDocument.hidden) {
                this.syncRefresh.pause();
            } else {
                this.scheduleSynchronization(true, true);
                this.scheduleConnectionHealthRefresh();
            }
        };
        this.onRefreshFocus = () => {
            if (
                !this.visibilityDocument.hidden &&
                (this.hasScheduledListSynchronization() ||
                    this.state.realtime !== "online" ||
                    this.syncRefresh.now() - this.lastFullSynchronizationStarted >=
                        CONSISTENCY_SYNCHRONIZATION_INTERVAL)
            ) {
                this.scheduleSynchronization(true, true);
                this.scheduleConnectionHealthRefresh();
            }
        };
        if (this.attention) {
            this.attention.setStateListener((snapshot) => {
                if (!this.destroyed) {
                    this.state.attention = snapshot;
                }
            });
        }
    }

    get selectedConversation() {
        return this.loadedConversation(this.state.selectedChannelId);
    }

    loadedConversation(channelId) {
        return (
            this.state.conversations.find(
                (item) =>
                    isRenderableConversation(item) &&
                    item.channel_id === channelId &&
                    !this.deletedConversationIds.has(channelId)
            ) || false
        );
    }

    canViewAttribution(conversation = this.selectedConversation) {
        return Boolean(
            conversation &&
                conversation.capabilities &&
                conversation.capabilities.view_attribution === true
        );
    }

    get currentUserId() {
        const user = this.state.bootstrap && this.state.bootstrap.user;
        return user && Number.isSafeInteger(user.id) && user.id > 0 ? user.id : false;
    }

    operationScopeKey() {
        const database =
            this.operationDatabase ||
            session.db ||
            session.db_name ||
            (window.location && window.location.host) ||
            "";
        return operationJournalStorageKey(database, this.currentUserId);
    }

    synchronizeOperationJournal() {
        const key = this.operationScopeKey();
        if (key === this.operationJournalKey) {
            return;
        }
        this.operationJournalKey = key;
        this.operationJournal = readOperationJournal(
            this.operationStorage,
            key,
            this.operationNow()
        );
    }

    operationIntent(signature) {
        this.synchronizeOperationJournal();
        const fingerprint = operationIntentFingerprint(signature);
        const now = this.operationNow();
        let entry = this.operationJournal.get(fingerprint);
        const recovered = Boolean(
            entry && now - entry.updatedAt <= OPERATION_JOURNAL_TTL_MS
        );
        if (recovered) {
            entry = {...entry, updatedAt: now};
        } else {
            entry = {
                fingerprint,
                requestId: makeClientRequestId(this.operationCrypto),
                updatedAt: now,
            };
        }
        this.operationJournal.delete(fingerprint);
        this.operationJournal.set(fingerprint, entry);
        writeOperationJournal(
            this.operationStorage,
            this.operationJournalKey,
            this.operationJournal,
            now
        );
        return {requestId: entry.requestId, recovered};
    }

    operationRequestId(signature) {
        return this.operationIntent(signature).requestId;
    }

    completeOperationIntent(signature, requestId) {
        this.synchronizeOperationJournal();
        const fingerprint = operationIntentFingerprint(signature);
        const entry = this.operationJournal.get(fingerprint);
        if (!entry || entry.requestId !== requestId) {
            return false;
        }
        this.operationJournal.delete(fingerprint);
        writeOperationJournal(
            this.operationStorage,
            this.operationJournalKey,
            this.operationJournal,
            this.operationNow()
        );
        return true;
    }

    isSending(channelId = this.state.selectedChannelId) {
        return Boolean(
            Number.isSafeInteger(channelId) &&
                this.state.sendingChannelIds.includes(channelId)
        );
    }

    setChannelSending(channelId, sending) {
        const current = new Set(this.state.sendingChannelIds);
        if (sending) {
            current.add(channelId);
        } else {
            current.delete(channelId);
        }
        this.state.sendingChannelIds = [...current];
    }

    registerConversationSelectionGuard(guard) {
        if (typeof guard !== "function") {
            return () => true;
        }
        this.conversationSelectionGuard = guard;
        return () => {
            if (this.conversationSelectionGuard === guard) {
                this.conversationSelectionGuard = null;
            }
        };
    }

    registerDraftAttachmentHandler(handler) {
        this.draftAttachmentHandler = handler;
        return () => {
            if (this.draftAttachmentHandler === handler) {
                this.draftAttachmentHandler = null;
            }
        };
    }

    async addDraftAttachment(source) {
        if (!this.draftAttachmentHandler) {
            this.notify("Abra a conversa para anexar o arquivo à mensagem.", {
                type: "warning",
            });
            return false;
        }
        return this.draftAttachmentHandler(source);
    }

    get responsibilityVisibleConversations() {
        return filterConversationsByResponsibility(
            this.state.conversations,
            this.state.filters.responsibility,
            this.currentUserId
        ).filter((item) => this.conversationMatchesPeopleAndTags(item));
    }

    get selectedFilterTagIds() {
        const ids = positiveFilterIds(this.state.filters.tagIds);
        return ids.length ? ids : positiveFilterIds([this.state.filters.tagId]);
    }

    get hasPeopleOrTagFilters() {
        return (
            this.state.filters.responsibility !== "all" ||
            Boolean(this.state.filters.responsibleId) ||
            this.selectedFilterTagIds.length > 0
        );
    }

    conversationMatchesPeopleAndTags(item) {
        if (
            !filterConversationsByResponsibility(
                [item],
                this.state.filters.responsibility,
                this.currentUserId
            ).length
        ) {
            return false;
        }
        const responsibleId = this.state.filters.responsibleId;
        if (
            responsibleId &&
            (!item.responsible || item.responsible.id !== responsibleId)
        ) {
            return false;
        }
        const tags = this.selectedFilterTagIds;
        return (
            !tags.length ||
            (Array.isArray(item.tags) && item.tags.some((tag) => tags.includes(tag.id)))
        );
    }

    /**
     * Filters whose result follows from the conversation's own fields.
     *
     * Unread, activity timing and search are "volatile": the agent's own work
     * (reading, completing the follow-up) can take the open conversation out of
     * them, so they alone never close it.
     *
     * @param {Object} item normalized conversation
     * @returns {Boolean} whether the item satisfies state, inbox, type,
     *   responsibility and tag filters
     */
    conversationMatchesStructuralFilters(item) {
        const filters = this.state.filters;
        if (!isRenderableConversation(item)) {
            return false;
        }
        if (filters.states.length && !filters.states.includes(item.state)) {
            return false;
        }
        if (
            filters.accountId &&
            (!item.account || item.account.id !== filters.accountId)
        ) {
            return false;
        }
        if (
            ["direct", "group"].includes(filters.conversationType) &&
            item.conversation_type !== filters.conversationType
        ) {
            return false;
        }
        return this.conversationMatchesPeopleAndTags(item);
    }

    get hasVolatileFilters() {
        const filters = this.state.filters;
        return Boolean(
            filters.unreadOnly ||
                filters.excludeMuted ||
                ACTIVITY_TIMINGS.has(filters.activityTiming) ||
                (filters.query || "").trim()
        );
    }

    get displayedConversationTotal() {
        const total =
            Number.isSafeInteger(this.state.conversationTotal) &&
            this.state.conversationTotal >= 0
                ? this.state.conversationTotal
                : 0;
        return Math.max(total, this.state.conversations.length);
    }

    get conversationStates() {
        const states = this.state.bootstrap && this.state.bootstrap.states;
        return ((states && states.conversation) || []).map((state) => {
            const metadata = conversationStateMeta(state.key);
            return {
                ...state,
                label: state.label || metadata.label,
                tone: metadata.tone,
            };
        });
    }

    get accounts() {
        return (this.state.bootstrap && this.state.bootstrap.accounts) || [];
    }

    get hasConnectionHealth() {
        return Boolean(this.state.connectionHealth);
    }

    get connectionHealth() {
        return this.state.connectionHealth;
    }

    get teams() {
        return (this.state.bootstrap && this.state.bootstrap.teams) || [];
    }

    get agents() {
        return (this.state.bootstrap && this.state.bootstrap.agents) || [];
    }

    get tags() {
        return (this.state.bootstrap && this.state.bootstrap.tags) || [];
    }

    reconcileTagCatalog(discoveredTags) {
        const bootstrap = this.state.bootstrap;
        if (!isPlainRecord(bootstrap) || !Array.isArray(discoveredTags)) {
            return false;
        }
        const discoveredById = new Map();
        for (const value of discoveredTags) {
            const tag = normalizeCatalogTag(value);
            if (tag) {
                // A selected conversation is the freshest server projection.
                // When an id occurs more than once, its last projected version wins.
                discoveredById.set(tag.id, tag);
            }
        }
        if (!discoveredById.size) {
            return false;
        }

        const catalogById = new Map();
        const existingOrder = [];
        const catalog = Array.isArray(bootstrap.tags) ? bootstrap.tags : [];
        for (const value of catalog) {
            const tag = normalizeCatalogTag(value);
            if (!tag) {
                continue;
            }
            if (!catalogById.has(tag.id)) {
                existingOrder.push(tag.id);
            }
            catalogById.set(tag.id, tag);
        }

        const appendedIds = [];
        for (const [tagId, tag] of discoveredById) {
            if (!catalogById.has(tagId)) {
                appendedIds.push(tagId);
            }
            // Conversation values intentionally replace a stale bootstrap version.
            catalogById.set(tagId, tag);
        }
        appendedIds.sort((leftId, rightId) =>
            compareCatalogTags(catalogById.get(leftId), catalogById.get(rightId))
        );
        const reconciled = [...existingOrder, ...appendedIds].map((tagId) =>
            catalogById.get(tagId)
        );
        const unchanged =
            catalog.length === reconciled.length &&
            reconciled.every((tag, index) => {
                const current = normalizeCatalogTag(catalog[index]);
                return (
                    current &&
                    current.id === tag.id &&
                    current.name === tag.name &&
                    current.color === tag.color
                );
            });
        if (unchanged) {
            return false;
        }
        bootstrap.tags = reconciled;
        return true;
    }

    get capabilities() {
        return (this.state.bootstrap && this.state.bootstrap.capabilities) || {};
    }

    get canCheckConnections() {
        return this.capabilities.check_connection_health === true;
    }

    call(
        method,
        args = [],
        kwargs = {},
        {silent = false, automatic = false, health = false} = {}
    ) {
        if (health && method !== "get_connection_health") {
            throw new TypeError("Health refresh is restricted to the safe projection.");
        }
        if (
            automatic &&
            ![
                "list_conversations",
                "get_conversation",
                "get_timeline",
                "get_conversation_avatars",
            ].includes(method)
        ) {
            throw new TypeError(
                "Automatic refresh is restricted to conversation reads."
            );
        }
        if (
            automatic &&
            (this.destroyed || this.syncCycleStopped || this.visibilityDocument.hidden)
        ) {
            return Promise.reject(new AutomaticRefreshDeferred());
        }
        const orm =
            (silent || automatic || health) && this.orm.silent
                ? this.orm.silent
                : this.orm;
        const request = orm.call(API_MODEL, method, args, kwargs);
        const result = health
            ? this.healthReads.read(request)
            : automatic
            ? this.syncReads.read(request)
            : request;
        if (method !== "get_conversation") {
            return result;
        }
        this.conversationDetailReadsInFlight += 1;
        if (
            !(automatic && this.syncFullActive) &&
            (this.syncAvatarActive || this.syncAvatarChannels.size)
        ) {
            this.scheduleSynchronization(false);
        }
        let finished = false;
        const finish = () => {
            if (!finished) {
                finished = true;
                this.conversationDetailReadsInFlight -= 1;
            }
        };
        const tracked = Promise.resolve(result).finally(finish);
        if (result && typeof result.abort === "function") {
            tracked.abort = (...options) => {
                const outcome = result.abort(...options);
                // Native abort({reject: false}) deliberately leaves the promise
                // unsettled; it no longer owns a detail snapshot.
                finish();
                return outcome;
            };
        }
        return tracked;
    }

    get syncTimer() {
        return this.syncRefresh.timer;
    }

    hasScheduledListSynchronization() {
        return (
            this.syncListPending ||
            (this.syncFullRequested &&
                (this.syncRefresh.pending || this.syncTimer !== null))
        );
    }

    notify(message, options = {}) {
        if (this.notification) {
            this.notification.add(message, options);
        }
    }

    async enableAttention() {
        if (!this.attention) {
            return false;
        }
        const snapshot = await this.attention.enable();
        if (!this.destroyed) {
            this.state.attention = snapshot;
        }
        return true;
    }

    async start() {
        this.started = true;
        window.addEventListener("pagehide", this.onPageHide);
        this.visibilityDocument.addEventListener(
            "visibilitychange",
            this.onRefreshVisibility
        );
        this.focusTarget.addEventListener("focus", this.onRefreshFocus);
        this.busService.addEventListener("notification", this.onNotification);
        this.busService.addEventListener("connect", this.onConnect);
        this.busService.addEventListener("reconnect", this.onReconnect);
        this.busService.addEventListener("reconnecting", this.onReconnecting);
        this.busService.addEventListener("disconnect", this.onDisconnect);
        const busStart = Promise.resolve()
            .then(() => this.busService.start())
            .then(() => {
                if (!this.destroyed) {
                    // Odoo 16 resolves busService.start() when its Worker is
                    // initialized, before the WebSocket emits `connect`.  Keep
                    // the honest `connecting` state until the transport itself
                    // proves connectivity.
                    this.startConsistencySynchronization();
                }
            })
            .catch(() => {
                if (!this.destroyed) {
                    this.state.realtime = "offline";
                    this.startConsistencySynchronization();
                }
            });
        await Promise.all([busStart, this.loadBootstrap()]);
    }

    destroy() {
        if (this.inboxPreferencesDirty && this.ownsDocumentContext()) {
            this.saveInboxPreferences();
        }
        this.cancelInboxPreferencesSave();
        this.rememberDocumentContext();
        this.destroyed = true;
        this.cancelSeenRetry();
        this.started = false;
        window.removeEventListener("pagehide", this.onPageHide);
        this.visibilityDocument.removeEventListener(
            "visibilitychange",
            this.onRefreshVisibility
        );
        this.focusTarget.removeEventListener("focus", this.onRefreshFocus);
        this.busService.removeEventListener("notification", this.onNotification);
        this.busService.removeEventListener("connect", this.onConnect);
        this.busService.removeEventListener("reconnect", this.onReconnect);
        this.busService.removeEventListener("reconnecting", this.onReconnecting);
        this.busService.removeEventListener("disconnect", this.onDisconnect);
        if (this.searchTimer !== null) {
            browser.clearTimeout(this.searchTimer);
        }
        this.syncRefresh.destroy();
        this.syncReads.destroy();
        this.syncAvatarChannels.clear();
        this.healthRefresh.destroy();
        this.healthReads.destroy();
        this.releaseSynchronizationWaiters(true);
        if (this.contactSearchTimer !== null) {
            this.contactTimer.clearTimeout(this.contactSearchTimer);
            this.contactSearchTimer = null;
        }
        if (this.companySearchTimer !== null) {
            this.companyTimer.clearTimeout(this.companySearchTimer);
            this.companySearchTimer = null;
        }
        this.stopConsistencySynchronization();
        this.stopConnectionHealthRefresh();
        this.conversationSelectionGuard = null;
        if (this.attention) {
            this.attention.destroy();
        }
    }

    inboxStateKey() {
        const database =
            this.operationDatabase ||
            session.db ||
            session.db_name ||
            (window.location && window.location.host) ||
            "";
        return inboxStateStorageKey(database, this.currentUserId);
    }

    filterPreferences() {
        const filters = this.state.filters;
        return {
            states: [...filters.states],
            accountId: filters.accountId,
            responsibility: filters.responsibility,
            responsibleId: filters.responsibleId,
            unreadOnly: filters.unreadOnly,
            excludeMuted: filters.excludeMuted,
            conversationType: filters.conversationType,
            tagIds: this.selectedFilterTagIds,
            activityTiming: filters.activityTiming,
        };
    }

    layoutPreferences() {
        return {
            detailsOpen: this.inboxLayout.detailsOpen,
            listView: this.inboxLayout.listView,
            sidePanel: this.inboxLayout.sidePanel,
            collapsedInboxes: {...this.inboxLayout.collapsedInboxes},
        };
    }

    cancelInboxPreferencesSave() {
        if (this.inboxPreferenceSaveTimer !== null) {
            this.inboxPreferenceTimer.clearTimeout(this.inboxPreferenceSaveTimer);
            this.inboxPreferenceSaveTimer = null;
        }
    }

    saveInboxPreferences() {
        this.cancelInboxPreferencesSave();
        // A replaced inbox never writes over the newer one's preferences.
        if (!this.inboxPreferencesLoaded || !this.ownsDocumentContext()) {
            return false;
        }
        this.inboxPreferencesDirty = false;
        return writeInboxPreferences(
            this.inboxPreferenceStorage,
            this.inboxStateKey(),
            // Directed navigation shows temporary neutral filters; keep the
            // saved ones until the agent changes a filter.
            this.persistFilters
                ? this.filterPreferences()
                : this.savedFilterPreferences,
            this.layoutPreferences()
        );
    }

    scheduleInboxPreferencesSave() {
        if (
            !this.inboxPreferencesLoaded ||
            this.destroyed ||
            !this.ownsDocumentContext()
        ) {
            return false;
        }
        this.cancelInboxPreferencesSave();
        this.inboxPreferencesDirty = true;
        this.inboxPreferenceSaveTimer = this.inboxPreferenceTimer.setTimeout(() => {
            this.inboxPreferenceSaveTimer = null;
            this.saveInboxPreferences();
        }, INBOX_STATE_SAVE_DELAY);
        return true;
    }

    noteFilterPreferenceChange() {
        this.persistFilters = true;
        this.scheduleInboxPreferencesSave();
    }

    rememberInboxLayout(patch = {}) {
        if (INBOX_LIST_VIEWS.has(patch.listView)) {
            this.inboxLayout.listView = patch.listView;
        }
        if (INBOX_SIDE_PANELS.has(patch.sidePanel)) {
            this.inboxLayout.sidePanel = patch.sidePanel;
        }
        if (typeof patch.detailsOpen === "boolean") {
            this.inboxLayout.detailsOpen = patch.detailsOpen;
        }
        if (isPlainRecord(patch.collapsedInboxes)) {
            this.inboxLayout.collapsedInboxes = Object.fromEntries(
                Object.entries(patch.collapsedInboxes).filter(
                    ([key, flag]) =>
                        flag === true &&
                        typeof key === "string" &&
                        key.startsWith("inbox:")
                )
            );
        }
        return this.scheduleInboxPreferencesSave();
    }

    onPageHide() {
        // Another tab may have saved newer preferences; write only this tab's
        // unsaved changes.
        if (this.inboxPreferencesDirty) {
            this.saveInboxPreferences();
        }
    }

    applyLayoutPreferences(layout) {
        this.inboxLayout = {
            listView: layout.listView || "grouped",
            collapsedInboxes: layout.collapsedInboxes || {},
            sidePanel: layout.sidePanel || "contact",
            detailsOpen:
                typeof layout.detailsOpen === "boolean"
                    ? layout.detailsOpen
                    : undefined,
        };
        // The details pane overlays the conversation on narrow screens; only
        // a wide viewport restores it open.
        if (typeof layout.detailsOpen === "boolean") {
            this.state.detailsOpen = layout.detailsOpen && window.innerWidth >= 1200;
        }
    }

    rememberedDocumentState() {
        const context = this.inboxContext;
        if (!context) {
            return false;
        }
        if (!this.currentUserId || context.userId !== this.currentUserId) {
            // Nothing of another user's session survives in this document.
            Object.assign(context, createInboxDocumentContext());
            return false;
        }
        return {
            query: typeof context.query === "string" ? context.query.slice(0, 256) : "",
            listScrollTop:
                Number.isFinite(context.listScrollTop) && context.listScrollTop > 0
                    ? context.listScrollTop
                    : 0,
            channelId:
                Number.isSafeInteger(context.channelId) && context.channelId > 0
                    ? context.channelId
                    : false,
            loadedCount:
                Number.isSafeInteger(context.loadedCount) && context.loadedCount > 0
                    ? Math.min(context.loadedCount, REALTIME_REFRESH_LIMIT)
                    : 0,
        };
    }

    restoreInboxPreferences(bootstrap, directed, navigation) {
        const preferences = readInboxPreferences(
            this.inboxPreferenceStorage,
            this.inboxStateKey(),
            bootstrap
        );
        this.applyLayoutPreferences(preferences.layout);
        this.savedFilterPreferences = preferences.filters;
        const remembered = this.rememberedDocumentState();
        if (this.inboxContext) {
            // A newer inbox of this document takes over the memory: an older
            // one being replaced must not overwrite it when it goes away.
            this.inboxContext.owner = this.documentOwner;
        }
        this.inboxPreferencesLoaded = true;
        // Action parameters win over saved filters and the remembered
        // conversation; their neutral filters are temporary.
        this.persistFilters = !directed;
        if (directed) {
            const preset = navigation.preset;
            Object.assign(this.state.filters, {
                states: [],
                accountId: false,
                query: "",
                responsibility: preset === "mine_unread" ? "mine" : "all",
                responsibleId: false,
                unreadOnly: Boolean(preset),
                excludeMuted: Boolean(preset),
                conversationType: false,
                tagId: false,
                tagIds: [],
                activityTiming: navigation.activityTiming || false,
            });
            return false;
        }
        Object.assign(this.state.filters, preferences.filters);
        if ("tagIds" in preferences.filters) {
            this.state.filters.tagId = false;
        }
        if (!remembered) {
            return false;
        }
        this.state.filters.query = remembered.query;
        return remembered;
    }

    ownsDocumentContext() {
        return Boolean(
            !this.inboxContext ||
                !this.inboxContext.owner ||
                this.inboxContext.owner === this.documentOwner
        );
    }

    /**
     * Publish this visit before the next action starts.
     *
     * The action service asks the leaving controller first; the next inbox
     * may boot before this one is destroyed and must read this state.
     *
     * @returns {Boolean} true: leaving is never blocked
     */
    handOffDocument() {
        if (this.inboxPreferencesDirty && this.ownsDocumentContext()) {
            this.saveInboxPreferences();
        }
        this.rememberDocumentContext();
        return true;
    }

    rememberDocumentContext() {
        if (
            !this.inboxPreferencesLoaded ||
            !this.inboxContext ||
            !this.ownsDocumentContext()
        ) {
            return false;
        }
        const restore = this.documentRestore;
        if (
            restore &&
            ["pending", "failed"].includes(restore.outcome) &&
            restore.filterRevision === this.filterRevision &&
            restore.selectionRevision === this.selectionRevision
        ) {
            // Leaving before the restoration finished, or after it could not
            // complete, keeps the previous memory while the agent has chosen
            // nothing since; a selection or filter edit supersedes it at once.
            return false;
        }
        Object.assign(this.inboxContext, {
            userId: this.currentUserId,
            channelId: this.state.selectedChannelId || false,
            query: this.state.filters.query || "",
            listScrollTop: this.listScrollTop,
            loadedCount: this.state.conversations.filter(
                (item) =>
                    isRenderableConversation(item) &&
                    item.channel_id !== this.preservedConversationChannelId
            ).length,
        });
        return true;
    }

    rememberListScroll(scrollTop) {
        if (Number.isFinite(scrollTop) && scrollTop >= 0) {
            this.listScrollTop = scrollTop;
        }
    }

    beginListLoad() {
        this.listLoadsInFlight += 1;
    }

    endListLoad() {
        this.listLoadsInFlight = Math.max(0, this.listLoadsInFlight - 1);
        if (!this.listLoadsInFlight) {
            const waiters = this.listIdleWaiters;
            this.listIdleWaiters = [];
            waiters.forEach((resolve) => resolve());
        }
    }

    waitForListIdle() {
        if (!this.listLoadsInFlight) {
            return Promise.resolve();
        }
        return new Promise((resolve) => this.listIdleWaiters.push(resolve));
    }

    waitForScheduledSynchronization() {
        if (!this.hasScheduledListSynchronization()) {
            return Promise.resolve(false);
        }
        // A queued cycle owns new waiters even if an older list is in flight.
        const waiters =
            this.syncFullRequested &&
            (this.syncRefresh.pending || this.syncTimer !== null)
                ? this.syncWaiters
                : this.syncActiveWaiters;
        return new Promise((resolve) => waiters.push(resolve));
    }

    releaseSynchronizationWaiters(all = false) {
        const waiters = this.syncActiveWaiters;
        this.syncActiveWaiters = [];
        if (all) {
            waiters.push(...this.syncWaiters);
            this.syncWaiters = [];
        }
        waiters.forEach((resolve) => resolve(true));
    }

    bumpFilterRevision() {
        this.filterRevision += 1;
        // A position saved for the previous list no longer applies.
        this.pendingListScrollTop = 0;
    }

    consumePendingListScroll() {
        const scrollTop = this.pendingListScrollTop;
        this.pendingListScrollTop = 0;
        return scrollTop;
    }

    stopConsistencySynchronization() {
        if (this.consistencySyncTimer !== null) {
            this.realtimeTimer.clearTimeout(this.consistencySyncTimer);
            this.consistencySyncTimer = null;
        }
    }

    startConsistencySynchronization() {
        if (!this.started || this.destroyed || this.consistencySyncTimer !== null) {
            return false;
        }
        this.consistencySyncTimer = this.realtimeTimer.setTimeout(() => {
            this.consistencySyncTimer = null;
            if (this.destroyed) {
                return;
            }
            // RPC remains the source of truth.  This refresh never claims that
            // realtime recovered; only a transport event can do that.
            this.scheduleSynchronization(false, true);
            if (this.state.realtime !== "online") {
                // Odoo normally retries an abnormal close itself.  Reissuing
                // start is idempotent and also recovers a client that joined a
                // SharedWorker while it was already stopped or reconnecting.
                Promise.resolve()
                    .then(() => this.busService.start())
                    .catch(() => {
                        if (!this.destroyed) {
                            this.state.realtime = "offline";
                        }
                    });
                this.refreshConnectionHealth();
            }
            this.startConsistencySynchronization();
        }, CONSISTENCY_SYNCHRONIZATION_INTERVAL);
        return true;
    }

    async loadBootstrap() {
        this.state.phase = "loading";
        try {
            const payload = await this.call("bootstrap");
            validateEnvelope(payload);
            this.state.bootstrap = payload;
            this.applyConnectionHealth(payload.connection_health);
            const navigation = this.initialNavigation;
            this.initialNavigation = false;
            const directed = Boolean(
                navigation &&
                    (navigation.channelId ||
                        navigation.activityTiming ||
                        navigation.preset)
            );
            const remembered = this.restoreInboxPreferences(
                payload,
                directed,
                navigation
            );
            // The saved or directed filters replace the defaults: a window a
            // realtime refresh fetched before bootstrap describes other filters.
            this.bumpFilterRevision();
            this.state.phase = "ready";
            // Nothing opens by itself: without a directed or remembered
            // conversation, the agent chooses what to open.
            // Captured before the first page: a search typed while it loads
            // (still inside its debounce) must cancel the restoration too.
            const revision = this.filterRevision;
            // Registered before the first page too: leaving while it loads
            // keeps the previous memory, since the agent chose nothing yet.
            const restore = remembered && this.beginDocumentRestore(revision);
            await this.loadConversations({reset: true});
            // Only the agent's own changes cancel what follows; a realtime
            // refresh that replaced the first page does not.
            if (directed && navigation.channelId) {
                if (this.filterRevision === revision) {
                    await this.openInitialConversation(navigation.channelId);
                }
            } else if (remembered) {
                await this.restoreDocumentState(remembered, restore);
            }
        } catch (error) {
            if (error instanceof RangeError) {
                this.state.phase = "unsupported";
                return;
            }
            this.state.phase = "error";
            this.notify(errorMessage(error), {type: "danger", title: "Contact Center"});
        }
    }

    async openInitialConversation(channelId) {
        if (this.destroyed || this.state.selectedChannelId) {
            return false;
        }
        const timelineRequest = this.timelineRequest;
        const selection = this.selectionRevision;
        const revision = this.filterRevision;
        // A filter edit or a selection by the agent cancels the opening; a
        // realtime refresh of the list does not.
        const current = () =>
            !this.destroyed &&
            this.filterRevision === revision &&
            this.timelineRequest === timelineRequest &&
            this.selectionRevision === selection &&
            !this.state.selectedChannelId;
        try {
            const payload = await this.call("get_conversation", [channelId]);
            if (!current()) {
                this.acceptLateConversationAnswer(payload, channelId);
                return false;
            }
            validateEnvelope(payload);
            if (
                !payload.item ||
                payload.item.channel_id !== channelId ||
                !this.replaceConversation(payload.item, {insert: true})
            ) {
                throw new TypeError("A conversa retornada pelo servidor é inválida.");
            }
            return this.selectConversation(channelId);
        } catch (error) {
            if (current()) {
                this.notify(errorMessage(error), {
                    type: "danger",
                    title: "Conversa indisponível",
                });
            }
            return false;
        }
    }

    async restoreConversationWindow(loadedCount, cancelled) {
        // Reload the same filtered window the agent had, never beyond the
        // realtime refresh bound, before restoring position or selection. A
        // realtime refresh may run meanwhile: wait for it and continue.
        const target = Math.min(loadedCount, REALTIME_REFRESH_LIMIT);
        // Enough page loads for the bound; a superseded page or a scheduled
        // refresh spends one too, so the wait stays bounded.
        const budget = REALTIME_REFRESH_LIMIT / LIST_LIMIT + 1;
        for (let pass = 0; ; pass += 1) {
            await this.waitForListIdle();
            if (cancelled()) {
                return "cancelled";
            }
            // Only a window the server returned for the current filters can
            // decide that a conversation left them. A failed request, or one
            // superseded by a refresh that failed, leaves it undecided.
            if (
                this.state.listPhase !== "ready" ||
                this.listWindowFilterRevision !== this.filterRevision
            ) {
                // A deletion or retention event superseded the window and
                // scheduled its refresh: wait for it rather than give up.
                if (
                    pass < budget &&
                    this.state.listPhase !== "error" &&
                    this.hasScheduledListSynchronization()
                ) {
                    await this.waitForScheduledSynchronization();
                    continue;
                }
                return "failed";
            }
            const loaded = this.state.conversations.filter(isRenderableConversation);
            if (loaded.length >= target || !this.state.conversationsHaveMore) {
                return "loaded";
            }
            if (pass >= budget) {
                return "failed";
            }
            await this.loadMoreConversations();
        }
    }

    beginDocumentRestore(filterRevision = this.filterRevision) {
        this.documentRestore = {
            outcome: "pending",
            filterRevision,
            selectionRevision: this.selectionRevision,
        };
        return this.documentRestore;
    }

    async restoreDocumentState(remembered, restore = this.beginDocumentRestore()) {
        // Only the agent's own actions abandon the restoration: a filter edit
        // (search included), choosing a conversation, or leaving.
        const cancelled = () =>
            this.destroyed ||
            this.filterRevision !== restore.filterRevision ||
            this.selectionRevision !== restore.selectionRevision;
        try {
            const windowOutcome = await this.restoreConversationWindow(
                remembered.loadedCount,
                cancelled
            );
            if (windowOutcome !== "loaded") {
                restore.outcome = windowOutcome;
                return false;
            }
            // The position comes back with the window, before the remembered
            // conversation's timeline, so a slow timeline never moves the list
            // under the agent's hands.
            this.listScrollTop = remembered.listScrollTop;
            this.pendingListScrollTop = remembered.listScrollTop;
            this.state.listScrollRestoreRequest += 1;
            if (remembered.channelId) {
                const outcome = await this.restoreRememberedConversation(
                    remembered.channelId,
                    cancelled
                );
                restore.outcome = outcome === "failed" ? "failed" : "done";
            } else {
                restore.outcome = "done";
            }
            return restore.outcome === "done";
        } finally {
            if (restore.outcome === "pending") {
                restore.outcome = "failed";
            }
        }
    }

    acceptsRestoredConversation(channelId, item, visible) {
        // Only a conversation inside the reloaded window, still matching the
        // effective filters with its fresh projection, is reopened.
        return Boolean(
            visible() &&
                item &&
                item.channel_id === channelId &&
                !this.readSinceListed(this.resolvedPreference(item)) &&
                this.replaceConversation(item) &&
                visible()
        );
    }

    /**
     * Whether the fresh conversation left a personal filter since the list.
     *
     * Read or muted in another tab meanwhile, it no longer satisfies the
     * effective filters; the volatile-filter tolerance is only for a
     * conversation already in use, not for one being restored.
     *
     * @param {Object} item the conversation just authorized by the server
     * @returns {Boolean}
     */
    readSinceListed(item) {
        const filters = this.state.filters;
        return Boolean(
            (filters.unreadOnly && !(item.unread_count > 0)) ||
                (filters.excludeMuted && item.preference && item.preference.muted)
        );
    }

    forgetRememberedConversation(channelId) {
        if (this.inboxContext && this.inboxContext.channelId === channelId) {
            this.inboxContext.channelId = false;
        }
    }

    /**
     * Reopen the remembered conversation after the server authorizes it again.
     *
     * @param {Number} channelId remembered conversation
     * @param {Function} cancelled returns true once the agent moved on
     * @returns {Promise<String>} "restored"; "unavailable" (forgotten: no
     *   access or outside the filters); "failed" (transient, remembered); or
     *   "cancelled" (the agent moved on; nothing is decided)
     */
    async restoreRememberedConversation(channelId, cancelled = () => false) {
        const visible = () =>
            this.responsibilityVisibleConversations.some(
                (item) => item.channel_id === channelId
            );
        if (this.destroyed || cancelled() || this.state.selectedChannelId) {
            return "cancelled";
        }
        // Only a conversation inside the reloaded, server-filtered window still
        // satisfies every effective filter (inbox, search, unread, activity...).
        if (!visible()) {
            this.forgetRememberedConversation(channelId);
            return "unavailable";
        }
        const timelineRequest = this.timelineRequest;
        const current = () =>
            !this.destroyed &&
            !cancelled() &&
            this.timelineRequest === timelineRequest &&
            !this.state.selectedChannelId;
        let payload = null;
        try {
            // The server authorizes again; the browser memory is only a hint.
            payload = await this.call("get_conversation", [channelId]);
        } catch (error) {
            if (!current()) {
                return "cancelled";
            }
            if (!accessWasRevoked(error)) {
                // A transient failure decides nothing about the memory.
                return "failed";
            }
            // Silent on purpose: the error must not reveal the conversation.
            this.forgetRememberedConversation(channelId);
            return "unavailable";
        }
        if (!current()) {
            this.acceptLateConversationAnswer(payload, channelId);
            return "cancelled";
        }
        try {
            validateEnvelope(payload);
        } catch (_error) {
            return "failed";
        }
        if (payload.item && payload.item.channel_id === channelId) {
            this.acceptPreferenceSnapshot(payload.item);
        }
        if (!this.acceptsRestoredConversation(channelId, payload.item, visible)) {
            this.forgetRememberedConversation(channelId);
            return "unavailable";
        }
        return this.reopenRestoredConversation(channelId);
    }

    async reopenRestoredConversation(channelId) {
        // Restoring is not reading: wait for the agent to interact.
        this.state.seenPausedChannelId = channelId;
        this.restoreDeniedChannelId = false;
        await this.selectConversation(channelId, {restored: true});
        // A timeline refused right after the authorization cleared it.
        const refused = this.restoreDeniedChannelId === channelId;
        this.restoreDeniedChannelId = false;
        return refused ? "unavailable" : "restored";
    }

    /**
     * Re-read the selection when the refresh kept it only from the cache.
     *
     * A preserved row, or a row kept from the cached tail beyond the refresh
     * bound, is only the previous projection. Every silent refresh caller (bus,
     * periodic, preferences, message actions...) re-checks the current item on
     * the server, which authorizes it and applies the structural filters.
     *
     * @param {Array} serverItems rows actually returned by this refresh
     * @returns {Promise<Boolean>} whether a revalidation ran
     */
    async revalidatePreservedSelection(serverItems = [], {automatic = false} = {}) {
        const channelId = this.state.selectedChannelId;
        if (
            !channelId ||
            !this.loadedConversation(channelId) ||
            (serverItems || []).some((item) => item && item.channel_id === channelId)
        ) {
            return false;
        }
        await this.refreshSelectedConversation({silent: true, automatic});
        return true;
    }

    applyConnectionHealth(value) {
        const health = normalizeConnectionHealth(value);
        this.state.connectionHealth = health || false;
        this.state.connectionHealthRevision += 1;
        this.updateConnectionHealthRefresh();
        return Boolean(health);
    }

    applyConnectionHealthItem(item) {
        const health = mergeConnectionHealth(this.state.connectionHealth, item);
        if (!health) {
            return false;
        }
        this.state.connectionHealth = health;
        this.state.connectionHealthRevision += 1;
        this.updateConnectionHealthRefresh();
        return true;
    }

    hasPendingConnectionHealthChecks() {
        const health = this.state.connectionHealth;
        return Boolean(health && health.summary && Number(health.summary.checking) > 0);
    }

    stopConnectionHealthRefresh() {
        if (this.healthSyncTimer !== null) {
            this.healthTimer.clearTimeout(this.healthSyncTimer);
            this.healthSyncTimer = null;
        }
        this.healthSyncAttempt = 0;
    }

    updateConnectionHealthRefresh() {
        if (this.hasPendingConnectionHealthChecks()) {
            this.scheduleConnectionHealthRefresh();
        } else {
            this.stopConnectionHealthRefresh();
        }
    }

    refreshConnectionHealth() {
        if (this.destroyed) {
            return Promise.resolve(false);
        }
        if (this.healthRefresh.running) {
            if (
                this.healthRequestBusRevision !== this.connectionHealthBusRevision ||
                this.healthInvalidationRequestRevision !==
                    this.connectionHealthInvalidationRevision
            ) {
                this.healthRefresh.schedule();
            }
            return this.healthRequest || Promise.resolve(false);
        }
        return this.healthRefresh.run();
    }

    async fetchConnectionHealth() {
        const busRevision = this.connectionHealthBusRevision;
        const invalidationRevision = this.connectionHealthInvalidationRevision;
        this.healthInvalidationRequestRevision = invalidationRevision;
        this.healthRequestBusRevision = busRevision;
        try {
            const payload = await this.call(
                "get_connection_health",
                [],
                {},
                {silent: true, health: true}
            );
            validateEnvelope(payload);
            if (this.destroyed) {
                return false;
            }
            if (this.connectionHealthBusRevision !== busRevision) {
                // Preserve bus items/snapshots without overwriting them with an
                // older read. Only an unapplied invalidation needs another RPC.
                const invalidated =
                    this.connectionHealthInvalidationRevision !== invalidationRevision;
                if (invalidated) {
                    this.healthRefresh.schedule();
                }
                return !invalidated;
            }
            return this.applyConnectionHealth(payload);
        } catch (_error) {
            return false;
        }
    }

    scheduleConnectionHealthRefresh() {
        if (this.healthSyncTimer !== null || this.destroyed) {
            return false;
        }
        const converging = this.hasPendingConnectionHealthChecks();
        if (
            converging &&
            this.healthSyncAttempt >= CONNECTION_HEALTH_REFRESH_DELAYS.length
        ) {
            return false;
        }
        const delay = converging
            ? CONNECTION_HEALTH_REFRESH_DELAYS[this.healthSyncAttempt++]
            : CONNECTION_HEALTH_INVALIDATION_DELAY;
        this.healthSyncTimer = this.healthTimer.setTimeout(async () => {
            this.healthSyncTimer = null;
            const refreshed = await this.refreshConnectionHealth();
            if (!refreshed && this.hasPendingConnectionHealthChecks()) {
                this.scheduleConnectionHealthRefresh();
            }
        }, delay);
        return true;
    }

    async checkConnectionHealth(connectionId = false) {
        if (
            this.state.connectionHealthPhase === "checking" ||
            (connectionId !== false &&
                (!Number.isSafeInteger(connectionId) || connectionId <= 0))
        ) {
            return false;
        }
        this.state.connectionHealthPhase = "checking";
        this.state.connectionHealthCheckingId = connectionId || 0;
        this.state.connectionHealthError = "";
        const busRevision = this.connectionHealthBusRevision;
        try {
            const payload = await this.call("check_connection_health", [
                connectionId || false,
            ]);
            validateEnvelope(payload);
            const snapshot =
                payload.connection_health ||
                (Array.isArray(payload.items) ? payload : false);
            this.stopConnectionHealthRefresh();
            if (this.connectionHealthBusRevision !== busRevision) {
                // A fast job can finish over the bus before this RPC response
                // arrives. Do not replace that newer event with the queued
                // snapshot returned by the request.
                this.scheduleConnectionHealthRefresh();
            } else if (snapshot) {
                this.applyConnectionHealth(snapshot);
            } else if (payload.item) {
                this.applyConnectionHealthItem(payload.item);
            }
            this.notify("Verificação de conexão agendada.", {type: "success"});
            return true;
        } catch (error) {
            this.state.connectionHealthError = errorMessage(error);
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Conexão não verificada",
            });
            return false;
        } finally {
            this.state.connectionHealthPhase = "idle";
            this.state.connectionHealthCheckingId = false;
        }
    }

    toggleConnectionHealth() {
        this.state.connectionHealthOpen = !this.state.connectionHealthOpen;
    }

    closeConnectionHealth() {
        this.state.connectionHealthOpen = false;
    }

    conversationFilters() {
        const filters = {};
        if (this.state.filters.states.length) {
            filters.states = [...this.state.filters.states];
        }
        if (this.state.filters.accountId) {
            filters.account_id = this.state.filters.accountId;
        }
        if (this.state.filters.query.trim()) {
            filters.query = this.state.filters.query.trim();
        }
        if (this.state.filters.responsibility !== "all") {
            filters.responsibility = this.state.filters.responsibility;
        }
        if (this.state.filters.responsibleId) {
            filters.responsible_id = this.state.filters.responsibleId;
        }
        if (this.state.filters.unreadOnly) {
            filters.unread_only = true;
        }
        if (this.state.filters.excludeMuted) {
            filters.exclude_muted = true;
        }
        if (["direct", "group"].includes(this.state.filters.conversationType)) {
            filters.conversation_type = this.state.filters.conversationType;
        }
        if (this.selectedFilterTagIds.length) {
            filters.tag_ids = this.selectedFilterTagIds;
        }
        if (
            ["all", "due", "overdue", "today", "planned"].includes(
                this.state.filters.activityTiming
            )
        ) {
            filters.activity_timing = this.state.filters.activityTiming;
        }
        return filters;
    }

    clearConversationSelection({closePanes = false} = {}) {
        const previousChannelId = this.state.selectedChannelId;
        this.restoringChannelId = false;
        this.cancelSeenRetry();
        this.timelineRequest += 1;
        this.resetTimelineContinuity();
        this.resetAttribution();
        this.resetProductivity();
        this.resetQuickReplies();
        this.state.selectedChannelId = false;
        this.state.seenPausedChannelId = false;
        this.state.timelineChannelId = false;
        this.state.messages = [];
        this.state.timelineHasMore = false;
        this.state.nextBeforeMessageId = false;
        this.state.timelineHasMoreForward = false;
        this.state.nextAfterChronologicalMessageId = false;
        this.state.timelineFirstUnreadMessageId = false;
        this.state.timelinePhase = "idle";
        this.state.replyTo = false;
        this.closeContactLinker();
        this.closeCompanyLinker();
        if (closePanes) {
            this.state.detailsOpen = false;
            this.state.mobilePane = "list";
        }
        this.dropHiddenRow(previousChannelId);
    }

    isCurrentConversationRequest(request) {
        return request === this.listRequest && !this.destroyed;
    }

    applyConversationPage(payload, {reset, silent, previousConversation}) {
        // The personal filters are checked again when the page is committed:
        // a cached tail captured before a selection change must follow the
        // current selection.
        const items = normalizedConversationItems(payload.items)
            .map((item) => this.resolvedPreference(item))
            .filter(
                (item) =>
                    !this.deletedConversationIds.has(item.channel_id) &&
                    this.conversationMatchesPeopleAndTags(item) &&
                    !this.hiddenByPersonalFilters(item)
            );
        if (reset) {
            this.state.conversations = items;
            this.preservedConversationChannelId = false;
            const previousIsMissing =
                previousConversation &&
                !items.some(
                    (item) => item.channel_id === previousConversation.channel_id
                );
            // A silent refresh keeps the open conversation only when its absence
            // may be pagination or a volatile filter; the next conversation
            // refresh re-checks the structural filters on the current item.
            if (
                silent &&
                previousIsMissing &&
                !this.deletedConversationIds.has(previousConversation.channel_id) &&
                (payload.has_more || this.hasVolatileFilters) &&
                this.conversationMatchesStructuralFilters(previousConversation)
            ) {
                this.state.conversations.push(previousConversation);
                this.preservedConversationChannelId = previousConversation.channel_id;
            }
        } else {
            this.mergeConversationPage(items);
        }
        if (payload.total !== false && payload.total !== undefined) {
            this.state.conversationTotal = payload.total;
        }
        this.state.conversationsHaveMore = Boolean(payload.has_more);
        this.state.nextConversationCursor = payload.next_cursor || false;
        this.state.listPhase = "ready";
    }

    /**
     * Append a next page to the loaded rows.
     *
     * @param {Array} items the page rows, already resolved and filtered
     */
    mergeConversationPage(items) {
        const byId = new Map(
            this.state.conversations
                .filter(
                    (item) =>
                        isRenderableConversation(item) &&
                        this.conversationMatchesPeopleAndTags(item) &&
                        !this.hiddenByPersonalFilters(item)
                )
                .map((item) => [item.channel_id, item])
        );
        for (const item of items) {
            if (item.channel_id === this.preservedConversationChannelId) {
                // Kept at the end only while no page had it: take its place.
                byId.delete(item.channel_id);
            }
            byId.set(item.channel_id, item);
        }
        this.state.conversations = [...byId.values()];
        if (
            this.preservedConversationChannelId &&
            items.some(
                (item) => item.channel_id === this.preservedConversationChannelId
            )
        ) {
            this.preservedConversationChannelId = false;
        }
    }

    reconcileConversationSelection({reset, previousSelected}) {
        const selectedStillVisible = this.responsibilityVisibleConversations.some(
            (item) => item.channel_id === previousSelected
        );
        // A selection that left the list is cleared, never replaced: another
        // conversation opens only when the agent chooses it.
        if (previousSelected && !selectedStillVisible && reset) {
            this.clearConversationSelection({
                closePanes: !this.responsibilityVisibleConversations.length,
            });
        }
    }

    conversationLoadFailed(error, request, silent) {
        if (!this.isCurrentConversationRequest(request)) {
            return false;
        }
        if (!silent) {
            this.state.listPhase = "error";
            this.notify(errorMessage(error), {
                type: error instanceof AutomaticRefreshDeferred ? "info" : "danger",
                title:
                    error instanceof AutomaticRefreshDeferred
                        ? "Atualização adiada"
                        : "Conversas indisponíveis",
            });
        }
        return false;
    }

    async loadConversations(options = {}) {
        if (this.destroyed) {
            return false;
        }
        this.beginListLoad();
        try {
            return await this.loadConversationsPage(options);
        } finally {
            this.endListLoad();
        }
    }

    async loadConversationsPage({
        reset = false,
        silent = false,
        automatic = false,
    } = {}) {
        if (this.destroyed) {
            return false;
        }
        // A silent load replacing a visible first page owns its visibility, as
        // a refresh does (L07-R16-01).
        const takesOver = silent && this.takeOverListLoad();
        const request = ++this.listRequest;
        const filterRevision = this.filterRevision;
        if (!silent) {
            this.state.listPhase = reset ? "loading" : "loading_more";
        }
        const cursor = reset ? false : this.state.nextConversationCursor;
        try {
            const payload = await this.call(
                "list_conversations",
                [],
                {
                    limit: LIST_LIMIT,
                    filters: this.conversationFilters(),
                    cursor,
                },
                {silent, automatic}
            );
            validateEnvelope(payload);
            this.rememberPagePreferences(payload.items);
            if (!this.isCurrentConversationRequest(request)) {
                return false;
            }
            const previousSelected = this.state.selectedChannelId;
            const previousConversation = this.selectedConversation;
            this.applyConversationPage(payload, {
                reset,
                silent,
                previousConversation,
            });
            this.listWindowFilterRevision = filterRevision;
            // A first page replaces every loaded row: no stale tail is left.
            this.listTailStale = this.listTailStale && !reset;
            this.reconcileConversationSelection({reset, previousSelected});
            if (reset && silent) {
                await this.revalidatePreservedSelection(payload.items, {automatic});
            }
            return true;
        } catch (error) {
            this.syncCycleFailed = this.syncCycleFailed || automatic;
            return this.conversationLoadFailed(error, request, silent && !takesOver);
        }
    }

    /**
     * A refresh replaces any list request in flight.
     *
     * Replacing a visible first page, it owns the visibility: its failure is
     * shown with the retry, never an endless skeleton (L07-R13-02). A page
     * being appended leaves the loading state.
     *
     * @returns {Boolean} whether a visible first page is replaced
     */
    takeOverListLoad() {
        if (this.state.listPhase === "loading_more") {
            this.state.listPhase = "ready";
        }
        return this.state.listPhase === "loading";
    }

    /**
     * The loaded rows a bounded refresh starts from.
     *
     * Rows loaded under other filters (a filter change still loading, or one
     * that failed) are no window of this list: neither their count nor their
     * tail is kept. -1: no page applied yet.
     *
     * @param {Boolean} takesOver whether the refresh replaces a visible load
     * @param {Number} filterRevision the filters the refresh loads
     * @returns {Array}
     */
    refreshCachedWindow(takesOver, filterRevision) {
        if (
            takesOver ||
            ![-1, filterRevision].includes(this.listWindowFilterRevision)
        ) {
            return [];
        }
        return this.state.conversations.filter(
            (item) =>
                isRenderableConversation(item) &&
                item.channel_id !== this.preservedConversationChannelId &&
                !this.hiddenByPersonalFilters(item)
        );
    }

    /**
     * The loaded rows a refresh may keep beyond its window.
     *
     * Under "Sem silenciadas" none: every refresh is a contiguous window from
     * the first page with a fresh cursor, so a conversation unmuted anywhere
     * is never skipped (emenda 2). Nor after "Marcar todas como lidas", until
     * a refresh is applied, or ever again after one with an unknown outcome
     * (L08 emendas). Otherwise the cached tail stays.
     *
     * @param {Array} cachedWindow the loaded rows
     * @returns {Array}
     */
    refreshableTail(cachedWindow) {
        return this.state.filters.excludeMuted ||
            this.listTailStale ||
            this.bulkReadUncertain
            ? []
            : cachedWindow;
    }

    async refreshLoadedConversations(options = {}) {
        if (this.destroyed) {
            return false;
        }
        this.beginListLoad();
        try {
            return await this.refreshLoadedConversationWindow(options);
        } finally {
            this.endListLoad();
        }
    }

    async refreshLoadedConversationWindow({silent = true, automatic = false} = {}) {
        if (this.destroyed) {
            return false;
        }
        const takesOver = this.takeOverListLoad();
        const request = ++this.listRequest;
        const filterRevision = this.filterRevision;
        const dropsStaleTail = this.listTailStale;
        const cachedWindow = this.refreshCachedWindow(takesOver, filterRevision);
        const loadedCount = cachedWindow.length;
        const cachedHasMore = this.state.conversationsHaveMore;
        const cachedNextCursor = this.state.nextConversationCursor;
        const targetCount = Math.min(
            REALTIME_REFRESH_LIMIT,
            Math.max(LIST_LIMIT, loadedCount)
        );
        const items = [];
        let cursor = false;
        let hasMore = true;
        let nextCursor = false;
        let total = false;
        const seenCursors = new Set();
        try {
            while (hasMore && items.length < targetCount) {
                const payload = await this.call(
                    "list_conversations",
                    [],
                    {
                        limit: Math.min(100, targetCount - items.length),
                        filters: this.conversationFilters(),
                        cursor,
                    },
                    {silent, automatic}
                );
                validateEnvelope(payload);
                this.rememberPagePreferences(payload.items);
                if (!this.isCurrentConversationRequest(request)) {
                    return false;
                }
                if (total === false && payload.total !== false) {
                    total = payload.total;
                }
                items.push(...(Array.isArray(payload.items) ? payload.items : []));
                hasMore = Boolean(payload.has_more);
                nextCursor = payload.next_cursor || false;
                if (!hasMore || !nextCursor) {
                    break;
                }
                const cursorKey = JSON.stringify(nextCursor);
                if (seenCursors.has(cursorKey)) {
                    throw new Error("O servidor repetiu o cursor de conversas.");
                }
                seenCursors.add(cursorKey);
                cursor = nextCursor;
            }
            const refreshedPage = realtimeConversationPage({
                items,
                hasMore,
                nextCursor,
                total,
                cachedWindow: this.refreshableTail(cachedWindow),
                cachedHasMore,
                cachedNextCursor,
                targetCount,
            });
            const previousSelected = this.state.selectedChannelId;
            const previousConversation = this.selectedConversation;
            this.applyConversationPage(
                {
                    schema_version: 1,
                    items: refreshedPage.items,
                    has_more: refreshedPage.hasMore,
                    next_cursor: refreshedPage.nextCursor,
                    total,
                },
                {reset: true, silent, previousConversation}
            );
            this.listWindowFilterRevision = filterRevision;
            this.listTailStale = this.listTailStale && !dropsStaleTail;
            this.reconcileConversationSelection({reset: true, previousSelected});
            await this.revalidatePreservedSelection(items, {automatic});
            return true;
        } catch (error) {
            this.syncCycleFailed = this.syncCycleFailed || automatic;
            return this.conversationLoadFailed(error, request, silent && !takesOver);
        }
    }

    loadMoreConversations() {
        if (["loading", "loading_more"].includes(this.state.listPhase)) {
            return Promise.resolve(false);
        }
        if (!this.state.conversationsHaveMore) {
            return Promise.resolve(false);
        }
        return this.loadConversations({reset: false});
    }

    setFilter(name, value) {
        if (!(name in this.state.filters)) {
            return false;
        }
        this.bumpFilterRevision();
        if (name === "states") {
            return this.setConversationStateFilters(value);
        }
        if (name === "responsibility") {
            if (!isResponsibilityScope(value)) {
                return false;
            }
            this.state.filters.responsibility = value;
            this.state.filters.responsibleId = false;
            this.noteFilterPreferenceChange();
            return this.loadConversations({reset: true});
        }
        let normalizedValue = value;
        if (BOOLEAN_FILTERS.has(name)) {
            normalizedValue = value === true;
        } else if (name === "conversationType") {
            normalizedValue = ["direct", "group"].includes(value) ? value : false;
        } else if (name === "responsibleId") {
            const id = Number(value);
            normalizedValue = Number.isSafeInteger(id) && id > 0 ? id : false;
            this.state.filters.responsibility = "all";
        } else if (name === "tagIds") {
            normalizedValue = positiveFilterIds(value);
            this.state.filters.tagId = false;
        } else if (name === "tagId") {
            const tagId = Number(value);
            normalizedValue = Number.isSafeInteger(tagId) && tagId > 0 ? tagId : false;
            this.state.filters.tagIds = normalizedValue ? [normalizedValue] : [];
        } else if (name === "activityTiming") {
            normalizedValue = ["all", "due", "overdue", "today", "planned"].includes(
                value
            )
                ? value
                : false;
        }
        this.state.filters[name] = normalizedValue;
        if (name === "query") {
            if (this.searchTimer !== null) {
                browser.clearTimeout(this.searchTimer);
            }
            this.searchTimer = browser.setTimeout(() => {
                this.searchTimer = null;
                this.loadConversations({reset: true});
            }, 260);
            return;
        }
        // Search text stays out of saved preferences.
        this.noteFilterPreferenceChange();
        return this.loadConversations({reset: true});
    }

    clearConversationFilters() {
        this.bumpFilterRevision();
        if (this.searchTimer !== null) {
            browser.clearTimeout(this.searchTimer);
            this.searchTimer = null;
        }
        Object.assign(this.state.filters, {
            states: [],
            accountId: false,
            responsibility: "all",
            responsibleId: false,
            unreadOnly: false,
            excludeMuted: false,
            conversationType: false,
            tagId: false,
            tagIds: [],
            activityTiming: false,
        });
        this.noteFilterPreferenceChange();
        return this.loadConversations({reset: true});
    }

    setConversationStateFilters(value) {
        const states = normalizeConversationStateFilters(value);
        if (states === false) {
            return false;
        }
        this.bumpFilterRevision();
        this.state.filters.states = states;
        this.noteFilterPreferenceChange();
        return this.loadConversations({reset: true});
    }

    toggleInboxDensity() {
        const density =
            this.state.inboxDensity === "compact" ? "comfortable" : "compact";
        this.state.inboxDensity = density;
        saveInboxDensityPreference(density, this.inboxDensityStorage);
        return density;
    }

    async normalizeStartPhone(accountId, phone) {
        const payload = await this.call("normalize_start_phone", [accountId, phone]);
        validateEnvelope(payload);
        if (!payload.normalized_phone || !payload.formatted_phone) {
            throw new TypeError("O número retornado pelo servidor é inválido.");
        }
        return payload;
    }

    async startConversation(accountId, phone, {isCurrent = () => true} = {}) {
        if (this.destroyed || this.startConversationPending) {
            return false;
        }
        if (
            !this.accounts.some(
                (account) =>
                    account.id === accountId && account.can_start_conversation === true
            )
        ) {
            throw new Error("Selecione uma caixa disponível para iniciar a conversa.");
        }
        // Guard BEFORE the write: refusing navigation during a recording must
        // not create a channel that the agent never meant to open.
        if (
            this.conversationSelectionGuard &&
            this.conversationSelectionGuard(false, {checkOnly: true}) === false
        ) {
            throw new Error(
                "Conclua ou descarte a gravação antes de iniciar outra conversa."
            );
        }
        this.startConversationPending = true;
        try {
            const payload = await this.call("start_conversation", [accountId, phone]);
            if (this.destroyed || !isCurrent()) {
                this.acceptLateConversationAnswer(payload);
                return false;
            }
            validateEnvelope(payload);
            if (
                !isRenderableConversation(payload.item) ||
                payload.item.channel_id !== payload.channel_id ||
                !payload.item.account ||
                payload.item.account.id !== accountId
            ) {
                throw new TypeError("A conversa retornada pelo servidor é inválida.");
            }
            if (
                this.conversationSelectionGuard &&
                this.conversationSelectionGuard(false, {checkOnly: true}) === false
            ) {
                this.acceptPreferenceSnapshot(payload.item);
                throw new Error(
                    "A conversa está disponível. Conclua a ação em andamento e abra novamente; ela será reutilizada."
                );
            }
            if (this.searchTimer !== null) {
                browser.clearTimeout(this.searchTimer);
                this.searchTimer = null;
            }
            // Resolved against the loaded row before the list is cleared: an
            // older answer never undoes a newer preference (L07-R13-01).
            const startedItem = this.resolvedPreference(
                normalizeConversationGroup(payload.item)
            );
            // Change only the list view, never the existing channel's state,
            // assignment or tags. In-flight pages must not hide this result.
            this.listRequest += 1;
            this.bumpFilterRevision();
            Object.assign(this.state.filters, {
                states: [],
                accountId,
                query: "",
                responsibility: "all",
                responsibleId: false,
                unreadOnly: false,
                excludeMuted: false,
                conversationType: false,
                tagId: false,
                tagIds: [],
                activityTiming: false,
            });
            this.noteFilterPreferenceChange();
            this.state.conversations = [];
            this.preservedConversationChannelId = false;
            this.state.conversationTotal = 1;
            this.state.conversationsHaveMore = false;
            this.state.nextConversationCursor = false;
            this.state.listPhase = "ready";
            this.replaceConversation(startedItem, {insert: true});
            await this.selectConversation(payload.channel_id);
            if (!this.destroyed && isCurrent()) {
                await this.loadConversations({reset: true, silent: true});
            }
            if (this.destroyed || !isCurrent()) {
                return false;
            }
            this.notify(
                "Conversa aberta. Os filtros foram ajustados para esta caixa; nenhuma mensagem foi enviada.",
                {type: "info"}
            );
            return payload;
        } finally {
            this.startConversationPending = false;
        }
    }

    /**
     * Whether a conversation may not be opened now.
     *
     * A deleted conversation never reopens; the list stays busy while "Marcar
     * todas como lidas" runs (L08).
     *
     * @param {Number} channelId the conversation to open
     * @returns {Boolean}
     */
    selectionRefused(channelId) {
        return (
            this.deletedConversationIds.has(channelId) ||
            (this.state.bulkReadPending && channelId !== this.state.selectedChannelId)
        );
    }

    async selectConversation(channelId, {preservePane = false, restored = false} = {}) {
        if (this.selectionRefused(channelId)) {
            return false;
        }
        if (
            this.state.selectedChannelId === channelId &&
            this.state.timelineChannelId === channelId &&
            this.state.messages.length
        ) {
            if (!restored) {
                // Choosing the open conversation again is an interaction.
                this.state.seenPausedChannelId = false;
            }
            if (!preservePane) {
                this.state.mobilePane = "conversation";
            }
            return true;
        }
        const changedConversation = this.state.selectedChannelId !== channelId;
        if (
            changedConversation &&
            this.conversationSelectionGuard &&
            this.conversationSelectionGuard(channelId) === false
        ) {
            // A refused switch changes nothing, the reading pause included.
            return false;
        }
        if (!restored) {
            // A selection by the agent is an interaction: reading resumes, and
            // any pending restoration gives way to the agent's choice.
            this.state.seenPausedChannelId = false;
            this.selectionRevision += 1;
        }
        const previousChannelId = this.state.selectedChannelId;
        this.state.selectedChannelId = channelId;
        if (changedConversation) {
            // The open conversation was kept only while it was open.
            this.dropHiddenRow(previousChannelId);
            this.dropPreservedRow(previousChannelId);
            this.cancelSeenRetry();
            // A latest-page bus refresh can overtake the initial reset request.
            // Clear the previous channel projection immediately and stamp the
            // empty timeline so that no response can merge two conversations.
            this.timelineRequest += 1;
            this.resetTimelineContinuity();
            this.state.timelineChannelId = channelId;
            this.state.messages = [];
            this.state.timelineHasMore = false;
            this.state.nextBeforeMessageId = false;
            this.state.timelineHasMoreForward = false;
            this.state.nextAfterChronologicalMessageId = false;
            this.state.timelineFirstUnreadMessageId = false;
            this.state.timelinePhase = "idle";
            this.resetAttribution(channelId);
            this.resetProductivity(channelId);
            this.resetQuickReplies(channelId);
        }
        this.state.replyTo = false;
        this.closeContactLinker();
        this.closeCompanyLinker();
        if (!preservePane) {
            this.state.mobilePane = "conversation";
        }
        // Until its first page applies, any current timeline request of a
        // restored conversation, a realtime refresh included, is its check.
        this.restoringChannelId = restored ? channelId : false;
        return this.loadTimeline({reset: true});
    }

    /**
     * Clear a restored conversation whose timeline the server now refuses.
     *
     * Authorized a moment ago, refused now: it leaves silently, as if it had
     * never been remembered, so the error reveals nothing about it.
     *
     * @param {Number} channelId the restored conversation
     */
    forgetRefusedRestoration(channelId) {
        this.restoreDeniedChannelId = channelId;
        const wasLoaded = Boolean(this.loadedConversation(channelId));
        this.state.conversations = this.state.conversations.filter(
            (item) => item.channel_id !== channelId
        );
        if (wasLoaded) {
            this.state.conversationTotal = Math.max(
                0,
                this.state.conversationTotal - 1
            );
        }
        this.clearConversationSelection({closePanes: true});
        this.forgetRememberedConversation(channelId);
    }

    resetProductivity(channelId = false) {
        this.productivityRequest += 1;
        this.state.productivity = productivityProjection(channelId);
    }

    resetQuickReplies(channelId = false) {
        this.quickReplyRequest += 1;
        this.state.quickReplies = {
            channelId,
            phase: "idle",
            query: "",
            items: [],
            error: "",
        };
    }

    isCurrentProductivityRequest(request, channelId) {
        return (
            request === this.productivityRequest &&
            channelId === this.state.selectedChannelId &&
            !this.destroyed
        );
    }

    canUseProductivity() {
        return ["followups", "scheduled_messages"].some(
            (capability) => this.capabilities[capability] === true
        );
    }

    async loadProductivity({force = false, notify = true} = {}) {
        const channelId = this.state.selectedChannelId;
        if (!channelId || this.destroyed || !this.canUseProductivity()) {
            return false;
        }
        const current = this.state.productivity;
        if (!force && current.channelId === channelId && current.phase === "ready") {
            return true;
        }
        const request = ++this.productivityRequest;
        this.state.productivity = {
            ...(current.channelId === channelId
                ? current
                : productivityProjection(channelId)),
            channelId,
            phase: "loading",
            error: "",
        };
        try {
            const payload = await this.call("get_productivity", [channelId]);
            validateEnvelope(payload);
            const projection = normalizeProductivity(payload, channelId);
            if (!this.isCurrentProductivityRequest(request, channelId)) {
                return false;
            }
            this.state.productivity = projection;
            return true;
        } catch (error) {
            if (!this.isCurrentProductivityRequest(request, channelId)) {
                return false;
            }
            const message = errorMessage(error);
            this.state.productivity = {
                ...this.state.productivity,
                channelId,
                phase: "error",
                error: message,
            };
            if (notify) {
                this.notify(message, {
                    type: "danger",
                    title: "Produtividade indisponível",
                });
            }
            return false;
        }
    }

    async searchQuickReplies(query = "", limit = QUICK_REPLY_LIMIT) {
        if (this.destroyed || this.capabilities.quick_replies !== true) {
            return false;
        }
        const channelId = Number(this.state.selectedChannelId);
        if (!Number.isSafeInteger(channelId) || channelId <= 0) {
            this.resetQuickReplies();
            return false;
        }
        const cleanQuery = typeof query === "string" ? query.trim() : "";
        const cleanLimit =
            Number.isSafeInteger(limit) && limit > 0
                ? Math.min(limit, QUICK_REPLY_LIMIT)
                : QUICK_REPLY_LIMIT;
        const request = ++this.quickReplyRequest;
        this.state.quickReplies = {
            ...this.state.quickReplies,
            channelId,
            phase: "loading",
            query: cleanQuery,
            error: "",
        };
        try {
            const payload = await this.call("search_quick_replies", [channelId], {
                query: cleanQuery,
                limit: cleanLimit,
            });
            validateEnvelope(payload);
            const items = normalizeQuickReplies(payload, channelId);
            if (
                request !== this.quickReplyRequest ||
                channelId !== this.state.selectedChannelId ||
                this.destroyed
            ) {
                return false;
            }
            this.state.quickReplies = {
                channelId,
                phase: "ready",
                query: cleanQuery,
                items,
                error: "",
            };
            return true;
        } catch (error) {
            if (
                request !== this.quickReplyRequest ||
                channelId !== this.state.selectedChannelId ||
                this.destroyed
            ) {
                return false;
            }
            const message = errorMessage(error);
            this.state.quickReplies = {
                ...this.state.quickReplies,
                channelId,
                phase: "error",
                query: cleanQuery,
                error: message,
            };
            this.notify(message, {
                type: "danger",
                title: "Respostas rápidas indisponíveis",
            });
            return false;
        }
    }

    productivityRequestId(signature) {
        return this.operationRequestId(signature);
    }

    async postInternalNote(body) {
        const channelId = this.state.selectedChannelId;
        const cleanBody = typeof body === "string" ? body.trim() : "";
        if (!channelId || !cleanBody || this.capabilities.internal_notes !== true) {
            return false;
        }
        const signature = `internal-note:${channelId}:${cleanBody}`;
        const requestId = this.productivityRequestId(signature);
        try {
            const payload = await this.call("post_internal_note", [
                channelId,
                cleanBody,
                requestId,
            ]);
            validateOperationEnvelope(payload, {
                channelId,
                requestId,
                requiredRecords: ["message"],
            });
            this.completeOperationIntent(signature, requestId);
            if (payload.message && this.state.selectedChannelId === channelId) {
                this.state.messages = mergeTimelineItems(
                    this.state.messages,
                    [payload.message],
                    {prepend: false}
                );
            } else if (this.state.selectedChannelId === channelId) {
                await this.refreshLatestTimeline();
            }
            await this.refreshLoadedConversations({silent: true});
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Nota interna não registrada",
                sticky: true,
            });
            return false;
        }
    }

    async scheduleFollowup(values) {
        const channelId = this.state.selectedChannelId;
        if (
            !channelId ||
            !isPlainRecord(values) ||
            this.capabilities.followups !== true
        ) {
            return false;
        }
        const requestValues = {...values};
        delete requestValues.client_request_id;
        const signature = `schedule-followup:${channelId}:${JSON.stringify([
            requestValues.activity_type_id || false,
            requestValues.date_deadline || "",
            requestValues.note || "",
            requestValues.summary || "",
            requestValues.user_id || false,
        ])}`;
        const requestId = this.productivityRequestId(signature);
        requestValues.client_request_id = requestId;
        try {
            const payload = await this.call("schedule_followup", [
                channelId,
                requestValues,
            ]);
            validateOperationEnvelope(payload, {
                channelId,
                requestId,
                requiredRecords: ["activity"],
            });
            this.completeOperationIntent(signature, requestId);
            await this.loadProductivity({force: true, notify: false});
            await this.refreshLoadedConversations({silent: true});
            this.notify("Follow-up agendado.", {type: "success"});
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Follow-up não agendado",
                sticky: true,
            });
            return false;
        }
    }

    async completeFollowup(activityId, feedback = "") {
        const channelId = this.state.selectedChannelId;
        const cleanId = Number(activityId);
        const cleanFeedback = typeof feedback === "string" ? feedback.trim() : "";
        if (
            !channelId ||
            !Number.isSafeInteger(cleanId) ||
            cleanId <= 0 ||
            this.capabilities.followups !== true
        ) {
            return false;
        }
        const signature = `complete-followup:${channelId}:${cleanId}:${cleanFeedback}`;
        const requestId = this.productivityRequestId(signature);
        try {
            const payload = await this.call("complete_followup", [
                channelId,
                cleanId,
                cleanFeedback,
                requestId,
            ]);
            validateOperationEnvelope(payload, {
                channelId,
                requestId,
                requiredRecords: ["activity"],
            });
            this.completeOperationIntent(signature, requestId);
            await this.loadProductivity({force: true, notify: false});
            await this.refreshLoadedConversations({silent: true});
            this.notify("Follow-up concluído.", {type: "success"});
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Follow-up não concluído",
                sticky: true,
            });
            return false;
        }
    }

    async scheduleMessage(body, scheduledAt) {
        const channelId = this.state.selectedChannelId;
        const cleanBody = typeof body === "string" ? body.trim() : "";
        if (!channelId || !cleanBody || this.capabilities.scheduled_messages !== true) {
            return false;
        }
        let utcValue = "";
        try {
            utcValue = localDateTimeToOdooUtc(scheduledAt);
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "warning",
                title: "Data inválida",
            });
            return false;
        }
        const signature = `schedule-message:${channelId}:${utcValue}:${cleanBody}`;
        const intent = this.operationIntent(signature);
        const requestId = intent.requestId;
        if (!intent.recovered) {
            const now = this.operationNow();
            try {
                if (Date.parse(utcValue) < now + 60 * 1000) {
                    throw new TypeError(
                        "Escolha uma data e hora com ao menos 1 minuto de antecedência."
                    );
                }
                if (Date.parse(utcValue) > now + 365 * 24 * 60 * 60 * 1000) {
                    throw new TypeError(
                        "O agendamento pode ser feito por até 365 dias."
                    );
                }
            } catch (error) {
                this.completeOperationIntent(signature, requestId);
                this.notify(errorMessage(error), {
                    type: "warning",
                    title: "Data inválida",
                });
                return false;
            }
        }
        try {
            const payload = await this.call("schedule_message", [
                channelId,
                cleanBody,
                utcValue,
                requestId,
            ]);
            validateOperationEnvelope(payload, {
                channelId,
                requestId,
                requiredRecords: ["scheduled_message"],
            });
            this.completeOperationIntent(signature, requestId);
            await this.loadProductivity({force: true, notify: false});
            this.notify("Mensagem agendada.", {type: "success"});
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Mensagem não agendada",
                sticky: true,
            });
            return false;
        }
    }

    async cancelScheduledMessage(scheduledMessageId) {
        const channelId = this.state.selectedChannelId;
        const cleanId = Number(scheduledMessageId);
        if (
            !channelId ||
            !Number.isSafeInteger(cleanId) ||
            cleanId <= 0 ||
            this.capabilities.scheduled_messages !== true
        ) {
            return false;
        }
        try {
            const payload = await this.call("cancel_scheduled_message", [
                channelId,
                cleanId,
            ]);
            validateEnvelope(payload);
            await this.loadProductivity({force: true, notify: false});
            this.notify("Mensagem agendada cancelada.", {type: "success"});
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Agendamento não cancelado",
                sticky: true,
            });
            return false;
        }
    }

    resetAttribution(channelId = false) {
        this.attributionRequest += 1;
        this.state.attribution = {
            channelId,
            phase: "idle",
            enabled: false,
            items: [],
            hasMore: false,
            nextCursor: false,
        };
    }

    isCurrentAttributionRequest(request, channelId) {
        return (
            request === this.attributionRequest &&
            channelId === this.state.selectedChannelId &&
            !this.destroyed
        );
    }

    async loadAttribution({silent = false, append = false, preserve = false} = {}) {
        const channelId = this.state.selectedChannelId;
        if (!channelId || this.destroyed) {
            return false;
        }
        const previous =
            this.state.attribution.channelId === channelId
                ? this.state.attribution
                : {
                      enabled: false,
                      items: [],
                      hasMore: false,
                      nextCursor: false,
                  };
        const previousItems = previous.items;
        const previousHasMore = previous.hasMore;
        const previousNextCursor = previous.nextCursor;
        const requestCursor = append ? previousNextCursor : false;
        const request = ++this.attributionRequest;
        this.state.attribution = {
            channelId,
            phase: append ? "loading_more" : "loading",
            enabled: previous.enabled,
            items: previousItems,
            hasMore: previousHasMore,
            nextCursor: previousNextCursor,
        };
        try {
            const payload = await this.call("get_attribution", [channelId], {
                cursor: requestCursor,
                limit: 3,
            });
            validateEnvelope(payload);
            const projection = normalizeAttributionProjection(payload);
            if (!this.isCurrentAttributionRequest(request, channelId)) {
                return false;
            }
            if (!projection || payload.channel_id !== channelId) {
                throw new TypeError("A resposta de origem é inválida.");
            }
            const mode = append
                ? "append"
                : preserve && previousItems.length
                ? "preserve"
                : "reset";
            const merged = mergeAttributionProjection(projection, previous, mode);
            this.state.attribution = {
                channelId,
                phase: "ready",
                ...merged,
            };
            return true;
        } catch (error) {
            if (!this.isCurrentAttributionRequest(request, channelId)) {
                return false;
            }
            this.state.attribution = {
                channelId,
                phase: "error",
                enabled: this.state.attribution.enabled,
                items: this.state.attribution.items,
                hasMore: this.state.attribution.hasMore,
                nextCursor: this.state.attribution.nextCursor,
            };
            if (!silent) {
                this.notify(errorMessage(error), {
                    type: "danger",
                    title: "Origem indisponível",
                });
            }
            return false;
        }
    }

    loadMoreAttribution() {
        if (
            this.state.attribution.phase === "loading_more" ||
            !this.state.attribution.hasMore ||
            !this.state.attribution.nextCursor
        ) {
            return Promise.resolve(false);
        }
        return this.loadAttribution({append: true});
    }

    isCurrentTimelineRequest(request, channelId) {
        return (
            request === this.timelineRequest &&
            channelId === this.state.selectedChannelId &&
            !this.destroyed
        );
    }

    resetTimelineForwardRecovery() {
        this.timelineForwardChannelId = false;
        this.timelineForwardCursor = false;
        this.timelineForwardPageCount = 0;
    }

    resetTimelineContinuity() {
        this.resetTimelineForwardRecovery();
        this.timelineContiguousChannelId = false;
        this.timelineContiguousCursor = false;
    }

    receivedTimelineCursor(channelId) {
        return this.timelineContiguousChannelId === channelId
            ? this.timelineContiguousCursor
            : false;
    }

    advanceTimelineContinuity(channelId, messageId) {
        if (!Number.isSafeInteger(messageId) || messageId <= 0) {
            return;
        }
        if (this.timelineContiguousChannelId !== channelId) {
            this.timelineContiguousChannelId = channelId;
            this.timelineContiguousCursor = messageId;
            return;
        }
        this.timelineContiguousCursor = Math.max(
            this.timelineContiguousCursor || 0,
            messageId
        );
    }

    contiguousTimelineItems(existing, channelId) {
        if (
            this.timelineContiguousChannelId !== channelId ||
            !this.timelineContiguousCursor
        ) {
            return [];
        }
        return existing.filter(
            (item) => item.message_id <= this.timelineContiguousCursor
        );
    }

    updateTimelineContinuityAfterPage({
        mode,
        channelId,
        latestReceivedMessageId,
        reanchorLatest,
        refreshHasOverlap,
        hasMore,
    }) {
        if (mode === "reset" || reanchorLatest) {
            this.resetTimelineContinuity();
            if (latestReceivedMessageId) {
                this.advanceTimelineContinuity(channelId, latestReceivedMessageId);
            }
            return;
        }
        if (!latestReceivedMessageId) {
            return;
        }
        if (mode === "older") {
            if (this.timelineContiguousChannelId !== channelId) {
                this.advanceTimelineContinuity(channelId, latestReceivedMessageId);
            }
            return;
        }
        if (refreshHasOverlap || !hasMore) {
            this.advanceTimelineContinuity(channelId, latestReceivedMessageId);
        }
    }

    shouldReanchorLatestTimeline({
        mode,
        hadMessages,
        incomingPage,
        hasMore,
        refreshHasOverlap,
    }) {
        return Boolean(
            mode === "refresh_latest" &&
                hadMessages &&
                incomingPage.length &&
                hasMore &&
                !refreshHasOverlap
        );
    }

    applyNewerTimelinePage(payload, incomingPage, existing, channelId) {
        const cursor = existing.find(
            (item) => item.message_id === this.state.nextAfterChronologicalMessageId
        );
        const last = incomingPage[incomingPage.length - 1];
        if (
            !cursor ||
            typeof payload.has_more_forward !== "boolean" ||
            incomingPage.some((item) => compareTimelineItems(item, cursor) <= 0) ||
            payload.next_after_chronological_message_id !==
                (last ? last.message_id : false) ||
            (payload.has_more_forward && !last)
        ) {
            throw new TypeError("O cursor cronológico de mensagens é inválido.");
        }
        this.state.messages = mergeTimelineItems(existing, incomingPage, {
            prepend: false,
        });
        this.state.timelineChannelId = channelId;
        if (!payload.has_more_forward) {
            this.advanceTimelineContinuity(
                channelId,
                payload.latest_received_message_id
            );
        }
        this.state.timelineHasMoreForward = Boolean(payload.has_more_forward);
        this.state.nextAfterChronologicalMessageId =
            payload.next_after_chronological_message_id || false;
        this.state.timelinePhase = "ready";
        this.reconcileReplySelection();
    }

    applyTimelineCursors(payload, mode, hadMessages, reanchorLatest) {
        if (mode !== "refresh_latest" || !hadMessages || reanchorLatest) {
            this.state.timelineHasMore = Boolean(payload.has_more);
            this.state.nextBeforeMessageId = payload.next_before_message_id || false;
        }
        if (mode !== "reset") {
            return;
        }
        this.state.timelineHasMoreForward = Boolean(payload.has_more_forward);
        this.state.nextAfterChronologicalMessageId =
            payload.next_after_chronological_message_id || false;
        this.state.timelineFirstUnreadMessageId =
            Number.isSafeInteger(payload.anchor_message_id) &&
            payload.anchor_message_id > 0
                ? payload.anchor_message_id
                : false;
    }

    applyTimelinePage(payload, mode, channelId) {
        if (this.restoringChannelId === channelId) {
            this.restoringChannelId = false;
        }
        const incoming = Array.isArray(payload.items) ? payload.items : [];
        const incomingPage = mergeTimelineItems([], incoming, {prepend: false});
        const ownsTimeline = this.state.timelineChannelId === channelId;
        const existing = ownsTimeline ? this.state.messages : [];
        const hadMessages = Boolean(existing.length);
        if (mode === "newer") {
            return this.applyNewerTimelinePage(
                payload,
                incomingPage,
                existing,
                channelId
            );
        }
        // Message IDs from mail.message are global and may contain arbitrary
        // gaps. Equality with at least one real ID is the only safe proof that
        // both pages form one continuous loaded window.
        const refreshHasOverlap = timelinePagesOverlap(
            this.contiguousTimelineItems(existing, channelId),
            incomingPage
        );
        const reanchorLatest = this.shouldReanchorLatestTimeline({
            mode,
            hadMessages,
            incomingPage,
            hasMore: Boolean(payload.has_more),
            refreshHasOverlap,
        });
        if (mode === "reset" || reanchorLatest) {
            this.state.messages = incomingPage;
        } else {
            this.state.messages = mergeTimelineItems(existing, incomingPage, {
                prepend: mode === "older",
            });
        }
        this.state.timelineChannelId = channelId;
        const latestReceivedMessageId = Number.isSafeInteger(
            payload.latest_received_message_id
        )
            ? payload.latest_received_message_id
            : Math.max(0, ...incomingPage.map((item) => item.message_id));
        this.updateTimelineContinuityAfterPage({
            mode,
            channelId,
            latestReceivedMessageId,
            reanchorLatest,
            refreshHasOverlap,
            hasMore: Boolean(payload.has_more),
        });
        // A live refresh is a projection of the newest page, not a new history
        // boundary. Keep the cursor of the oldest page already loaded so that a
        // bus notification cannot discard the agent's pagination position. If
        // there is no real overlap, reanchor and expose the newest page's cursor
        // instead of presenting two disconnected history islands as continuous.
        this.applyTimelineCursors(payload, mode, hadMessages, reanchorLatest);
        this.state.timelinePhase = "ready";
        this.reconcileReplySelection();
    }

    timelineNeedsForwardRecovery(payload, channelId) {
        if (this.timelineForwardChannelId === channelId && this.timelineForwardCursor) {
            return true;
        }
        // A delayed or imported message can arrive behind the visible date
        // window. Catch it by ingestion ID even when the newest page overlaps.
        return (
            payload.has_unloaded_received === true ||
            this.timelineHeadHasContiguousGap(payload, channelId)
        );
    }

    timelineHeadHasContiguousGap(payload, channelId) {
        if (
            this.state.timelineChannelId !== channelId ||
            !this.state.messages.length ||
            !payload.has_more
        ) {
            return false;
        }
        const incoming = mergeTimelineItems([], payload.items, {prepend: false});
        return Boolean(
            incoming.length &&
                !timelinePagesOverlap(
                    this.contiguousTimelineItems(this.state.messages, channelId),
                    incoming
                )
        );
    }

    applyForwardTimelineItems(items, channelId, nextAfterMessageId) {
        const oldest = this.state.messages[0];
        const visibleItems = oldest
            ? items.filter((item) => compareTimelineItems(item, oldest) >= 0)
            : items;
        if (visibleItems.length < items.length && !this.state.timelineHasMore) {
            // A previously complete window can gain older history. Keep that
            // history reachable even when a live head refresh overlaps fully.
            this.state.timelineHasMore = true;
            this.state.nextBeforeMessageId = oldest.message_id;
        }
        this.state.messages = mergeTimelineItems(this.state.messages, visibleItems, {
            prepend: false,
        });
        this.state.timelineChannelId = channelId;
        this.advanceTimelineContinuity(channelId, nextAfterMessageId);
        this.state.timelinePhase = "ready";
        this.reconcileReplySelection();
    }

    beginTimelineForwardRecovery(channelId) {
        const continuingRecovery = Boolean(
            this.timelineForwardChannelId === channelId && this.timelineForwardCursor
        );
        const afterMessageId = continuingRecovery
            ? this.timelineForwardCursor
            : this.timelineContiguousChannelId === channelId &&
              this.timelineContiguousCursor;
        if (!Number.isSafeInteger(afterMessageId) || afterMessageId <= 0) {
            throw new TypeError(
                "O ponto confirmado da paginação incremental é inválido."
            );
        }
        if (!continuingRecovery) {
            this.timelineForwardChannelId = channelId;
            this.timelineForwardCursor = afterMessageId;
            this.timelineForwardPageCount = 0;
        }
        return afterMessageId;
    }

    async fetchForwardTimelinePage(
        request,
        channelId,
        afterMessageId,
        limit,
        options = {}
    ) {
        const payload = await this.call(
            "get_timeline",
            [channelId],
            {
                after_message_id: afterMessageId,
                limit,
            },
            options
        );
        validateEnvelope(payload);
        if (!this.isCurrentTimelineRequest(request, channelId)) {
            return false;
        }
        if (payload.channel_id !== channelId) {
            throw new TypeError("A conversa da paginação incremental é inválida.");
        }
        return forwardTimelinePage(payload, afterMessageId);
    }

    async fetchCurrentTimelineHead(
        request,
        channelId,
        limit,
        invalidMessage,
        options = {}
    ) {
        const payload = await this.call(
            "get_timeline",
            [channelId],
            {
                before_message_id: false,
                limit,
                known_received_message_id: this.receivedTimelineCursor(channelId),
            },
            options
        );
        validateEnvelope(payload);
        if (!this.isCurrentTimelineRequest(request, channelId)) {
            return false;
        }
        if (payload.channel_id !== channelId) {
            throw new TypeError(invalidMessage);
        }
        return payload;
    }

    async reanchorTimelineAfterForwardLimit(request, channelId, limit, options = {}) {
        const payload = await this.fetchCurrentTimelineHead(
            request,
            channelId,
            limit,
            "A conversa da recentralização de mensagens é inválida.",
            options
        );
        if (!payload) {
            return false;
        }
        this.resetTimelineForwardRecovery();
        this.applyTimelinePage(payload, "reset", channelId);
        this.notify(
            "A conversa recebeu um volume alto durante a ausência. O histórico foi recentralizado e continua disponível ao carregar mensagens anteriores.",
            {type: "warning", title: "Histórico recentralizado"}
        );
        return true;
    }

    async finishTimelineForwardRecovery(request, channelId, limit, options = {}) {
        const payload = await this.fetchCurrentTimelineHead(
            request,
            channelId,
            limit,
            "A conversa da atualização final de mensagens é inválida.",
            options
        );
        if (!payload) {
            return false;
        }
        if (this.timelineHeadHasContiguousGap(payload, channelId)) {
            // More than one head window arrived while the bounded catch-up was
            // running. Preserve the confirmed cursor for the next fenced cycle.
            this.scheduleSynchronization(false, true, {urgent: true});
            return true;
        }
        this.resetTimelineForwardRecovery();
        this.applyTimelinePage(payload, "refresh_latest", channelId);
        return true;
    }

    async recoverTimelineGap(request, channelId, limit, options = {}) {
        let afterMessageId = this.beginTimelineForwardRecovery(channelId);
        let hasMore = true;
        while (hasMore && this.timelineForwardPageCount < TIMELINE_FORWARD_MAX_PAGES) {
            const page = await this.fetchForwardTimelinePage(
                request,
                channelId,
                afterMessageId,
                limit,
                options
            );
            if (!page) {
                return false;
            }
            this.applyForwardTimelineItems(
                page.items,
                channelId,
                page.nextAfterMessageId || afterMessageId
            );
            hasMore = page.hasMore;
            afterMessageId = page.nextAfterMessageId || afterMessageId;
            this.timelineForwardCursor = afterMessageId;
            this.timelineForwardPageCount += 1;
        }
        if (hasMore) {
            // Bound the aggregate automatic catch-up, not only one RPC chain.
            // A very large offline backlog is reanchored to a fresh head; the
            // operator can still page backwards without an unbounded DOM/RPC loop.
            return this.reanchorTimelineAfterForwardLimit(
                request,
                channelId,
                limit,
                options
            );
        }
        return this.finishTimelineForwardRecovery(request, channelId, limit, options);
    }

    scheduleTimelineForwardRetry(channelId) {
        if (
            this.timelineForwardChannelId !== channelId ||
            !this.timelineForwardCursor ||
            this.timelineForwardPageCount >= TIMELINE_FORWARD_MAX_PAGES
        ) {
            return false;
        }
        // Failed RPC/validation attempts consume the same aggregate budget as
        // successful forward pages, so a broken endpoint cannot create a retry loop.
        this.timelineForwardPageCount += 1;
        this.scheduleSynchronization(false, true, {urgent: true});
        return true;
    }

    timelineLoadFailed(error, request, channelId, silent) {
        // Only the current request decides: a late refusal never clears a
        // newer choice of the agent.
        if (!this.isCurrentTimelineRequest(request, channelId)) {
            return false;
        }
        if (this.restoringChannelId === channelId && accessWasRevoked(error)) {
            this.forgetRefusedRestoration(channelId);
            return false;
        }
        if (silent) {
            this.scheduleTimelineForwardRetry(channelId);
            return false;
        }
        this.state.timelinePhase = "error";
        this.notify(errorMessage(error), {
            type: "danger",
            title: "Mensagens indisponíveis",
        });
        return false;
    }

    async loadTimeline({
        reset = true,
        mode = reset ? "reset" : "older",
        silent = false,
        limit = TIMELINE_LIMIT,
        anchorUnread = true,
        automatic = false,
    } = {}) {
        const channelId = this.state.selectedChannelId;
        if (!channelId || this.destroyed) {
            return false;
        }
        if (!TIMELINE_MODES.has(mode)) {
            throw new TypeError(`Unsupported timeline load mode: ${mode}`);
        }
        const request = ++this.timelineRequest;
        if (!silent) {
            this.state.timelinePhase = timelineLoadingPhase(mode);
        }
        try {
            const firstUnreadMessageId = anchorUnread
                ? initialUnreadMessageId(mode, this.selectedConversation)
                : false;
            const query = timelineQuery(
                mode,
                limit,
                this.state.nextBeforeMessageId,
                this.state.nextAfterChronologicalMessageId,
                firstUnreadMessageId
            );
            if (mode === "refresh_latest") {
                query.known_received_message_id =
                    this.receivedTimelineCursor(channelId);
            }
            const payload = await this.call("get_timeline", [channelId], query, {
                silent,
                automatic,
            });
            validateEnvelope(payload);
            if (!this.isCurrentTimelineRequest(request, channelId)) {
                return false;
            }
            if (payload.channel_id !== channelId || !Array.isArray(payload.items)) {
                throw new TypeError("A conversa retornada pelo servidor é inválida.");
            }
            if (
                mode === "refresh_latest" &&
                this.timelineNeedsForwardRecovery(payload, channelId)
            ) {
                return await this.recoverTimelineGap(request, channelId, limit, {
                    silent,
                    automatic,
                });
            }
            this.applyTimelinePage(payload, mode, channelId);
            return true;
        } catch (error) {
            this.syncCycleFailed = this.syncCycleFailed || automatic;
            return this.timelineLoadFailed(error, request, channelId, silent);
        }
    }

    loadOlderMessages() {
        if (
            ["loading", "loading_more", "loading_newer"].includes(
                this.state.timelinePhase
            ) ||
            (this.timelineForwardChannelId === this.state.selectedChannelId &&
                this.timelineForwardCursor) ||
            !this.state.timelineHasMore
        ) {
            return Promise.resolve(false);
        }
        return this.loadTimeline({reset: false});
    }

    loadNewerMessages() {
        if (
            ["loading", "loading_more", "loading_newer"].includes(
                this.state.timelinePhase
            ) ||
            !this.state.timelineHasMoreForward ||
            !this.state.nextAfterChronologicalMessageId
        ) {
            return Promise.resolve(false);
        }
        return this.loadTimeline({reset: false, mode: "newer"});
    }

    jumpToLatest() {
        return this.loadTimeline({reset: true, anchorUnread: false});
    }

    refreshLatestTimeline({automatic = false} = {}) {
        if (this.state.timelineHasMoreForward) {
            return Promise.resolve(false);
        }
        return this.loadTimeline({
            mode: "refresh_latest",
            silent: true,
            limit: TIMELINE_REFRESH_LIMIT,
            automatic,
        });
    }

    cancelSeenRetry() {
        this.seenRetryToken += 1;
        this.confirmedSeenPointer = false;
        if (this.seenRetryTimer !== null) {
            this.realtimeTimer.clearTimeout(this.seenRetryTimer);
            this.seenRetryTimer = null;
        }
    }

    scheduleSeenRetry(channelId, messageId, token, attempt) {
        if (
            this.destroyed ||
            token !== this.seenRetryToken ||
            channelId !== this.state.selectedChannelId
        ) {
            return false;
        }
        if (attempt >= SEEN_RETRY_DELAYS.length) {
            this.scheduleSynchronization(false, false, {urgent: true});
            return false;
        }
        this.seenRetryTimer = this.realtimeTimer.setTimeout(async () => {
            this.seenRetryTimer = null;
            await this.persistSeenPointer(channelId, messageId, token, attempt + 1);
        }, SEEN_RETRY_DELAYS[attempt]);
        return true;
    }

    persistSeenPointer(channelId, messageId, token, attempt) {
        if (
            this.destroyed ||
            token !== this.seenRetryToken ||
            channelId !== this.state.selectedChannelId ||
            this.suspendedSeenChannels.has(channelId) ||
            this.deletedConversationIds.has(channelId)
        ) {
            return Promise.resolve(false);
        }
        const pending = this.pendingSeenRequests.get(channelId) || new Set();
        const request = {
            messageId,
            token,
            promise: this.writeSeenPointer(channelId, messageId, token, attempt),
        };
        pending.add(request);
        this.pendingSeenRequests.set(channelId, pending);
        const settled = () => {
            pending.delete(request);
            if (!pending.size) {
                this.pendingSeenRequests.delete(channelId);
            }
        };
        request.promise.then(settled, settled);
        return request.promise;
    }

    async writeSeenPointer(channelId, messageId, token, attempt) {
        try {
            const payload = await this.call("mark_seen", [channelId, messageId]);
            if (
                !payload ||
                payload.channel_id !== channelId ||
                payload.message_id !== messageId
            ) {
                throw new TypeError("O servidor não confirmou a leitura da mensagem.");
            }
            if (
                this.destroyed ||
                token !== this.seenRetryToken ||
                channelId !== this.state.selectedChannelId
            ) {
                return false;
            }
            const latest = this.state.messages[this.state.messages.length - 1];
            const seen = this.state.messages.find(
                (item) => item.message_id === messageId
            );
            const conversation = this.selectedConversation;
            const knownLatest = conversation && conversation.last_message;
            const lastActivityAt = conversation && conversation.last_activity_at;
            this.confirmedSeenPointer = {channelId, messageId};
            if (
                !this.state.timelineHasMoreForward &&
                seen &&
                [latest, knownLatest].every(
                    (item) =>
                        !item ||
                        item.message_id === seen.message_id ||
                        compareTimelineItems(
                            {
                                message_id: item.message_id,
                                // Compact list previews carry their date on the
                                // conversation, not inside last_message.
                                date: item.date || lastActivityAt,
                            },
                            seen
                        ) <= 0
                )
            ) {
                if (conversation) {
                    conversation.unread_count = 0;
                    conversation.first_unread_message_id = false;
                }
                this.state.timelineFirstUnreadMessageId = false;
            } else {
                this.scheduleSynchronization(false, true, {urgent: true});
            }
            return true;
        } catch (_error) {
            this.scheduleSeenRetry(channelId, messageId, token, attempt);
            return false;
        }
    }

    markSeen(messageId) {
        const channelId = this.state.selectedChannelId;
        if (
            !channelId ||
            !Number.isSafeInteger(messageId) ||
            messageId <= 0 ||
            this.suspendedSeenChannels.has(channelId) ||
            this.deletedConversationIds.has(channelId) ||
            this.state.seenPausedChannelId === channelId ||
            this.destroyed
        ) {
            return Promise.resolve(false);
        }
        const pending = [...(this.pendingSeenRequests.get(channelId) || [])].find(
            (request) =>
                request.messageId === messageId && request.token === this.seenRetryToken
        );
        if (pending) {
            return pending.promise;
        }
        const confirmed = this.confirmedSeenPointer || {};
        if (confirmed.channelId === channelId && confirmed.messageId === messageId) {
            return Promise.resolve(true);
        }
        this.cancelSeenRetry();
        return this.persistSeenPointer(channelId, messageId, this.seenRetryToken, 0);
    }

    resumeSeen(channelId = this.state.selectedChannelId) {
        if (!channelId || this.state.seenPausedChannelId !== channelId) {
            return false;
        }
        this.state.seenPausedChannelId = false;
        return true;
    }

    setReply(message) {
        const policy = conversationUiPolicy(this.selectedConversation);
        this.state.replyTo =
            policy.allow_reply &&
            message &&
            Number.isSafeInteger(message.message_id) &&
            message.message_id > 0 &&
            message.actions &&
            message.actions.reply === true
                ? message
                : false;
    }

    clearReply() {
        this.state.replyTo = false;
    }

    reconcileReplySelection() {
        if (!this.state.replyTo) {
            return;
        }
        const current = this.state.messages.find(
            (message) => message.message_id === this.state.replyTo.message_id
        );
        if (
            !conversationUiPolicy(this.selectedConversation).allow_reply ||
            !current ||
            !current.actions ||
            current.actions.reply !== true
        ) {
            this.state.replyTo = false;
            return;
        }
        this.state.replyTo = current;
    }

    async uploadMedia(
        file,
        clientUploadId,
        channelId = this.state.selectedChannelId,
        signal = undefined,
        metadata = {}
    ) {
        const conversation = this.state.conversations.find(
            (item) => item.channel_id === channelId
        );
        if (!conversationUiPolicy(conversation).allow_attachments || !file) {
            throw new Error("Esta conversa não está disponível para anexos.");
        }
        this.activeUploads += 1;
        this.state.uploading = true;
        try {
            const requestValues = {
                channelId,
                file,
                clientUploadId,
                signal,
            };
            if (
                Number.isSafeInteger(metadata.durationSeconds) &&
                metadata.durationSeconds > 0
            ) {
                requestValues.isVoiceNote = metadata.isVoiceNote === true;
                requestValues.durationSeconds = metadata.durationSeconds;
            }
            return await this.uploadRequest(requestValues);
        } finally {
            this.activeUploads = Math.max(0, this.activeUploads - 1);
            this.state.uploading = Boolean(this.activeUploads);
        }
    }

    prepareSendContext(
        conversation,
        cleanBody,
        cleanMediaRefs,
        requestedReplyId,
        structuredContent = false,
        replayRequestId = false
    ) {
        const policy = conversationUiPolicy(conversation);
        if (
            !canStartSend(
                conversation,
                cleanBody || structuredContent,
                cleanMediaRefs,
                this.isSending(conversation && conversation.channel_id)
            )
        ) {
            return false;
        }
        // Repeating an uncertain operation asks the server to acknowledge its
        // original UUID. The application checks that match before current card
        // capabilities, and still validates all current rules for a new send.
        if (replayRequestId) {
            return {replyId: requestedReplyId || false};
        }
        if (
            (cleanMediaRefs.length && !policy.allow_attachments) ||
            (structuredContent &&
                (cleanMediaRefs.length ||
                    !Object.keys(
                        outboundStructuredCapabilities(conversation.capabilities)
                    ).includes(structuredContent.type)))
        ) {
            return false;
        }
        return this.prepareReplyContext(policy, requestedReplyId);
    }

    prepareReplyContext(policy, requestedReplyId) {
        const replyId =
            requestedReplyId === undefined
                ? this.state.replyTo && this.state.replyTo.message_id
                : requestedReplyId;
        if (!replyId) {
            return {replyId: false};
        }
        const target = this.state.messages.find(
            (message) => message.message_id === replyId
        );
        // A pending operation already captured this target while it was visible.
        // Pagination after a conversation switch does not invalidate that ID;
        // the server still enforces target ownership and reply policy.
        if (
            !target &&
            requestedReplyId !== undefined &&
            policy.allow_reply &&
            Number.isSafeInteger(replyId) &&
            replyId > 0
        ) {
            return {replyId};
        }
        if (
            !policy.allow_reply ||
            !target ||
            !target.actions ||
            target.actions.reply !== true
        ) {
            if (requestedReplyId === undefined) {
                this.state.replyTo = false;
            }
            return false;
        }
        return {replyId: target.message_id};
    }

    async sendMessage(body, mediaRefs = [], options = {}) {
        if (this.destroyed) {
            return sendOutcome(options, false, true);
        }
        const conversation = options.channelId
            ? this.state.conversations.find(
                  (item) => item.channel_id === options.channelId
              )
            : this.selectedConversation;
        const cleanBody = typeof body === "string" ? body.trim() : "";
        const cleanMediaRefs = normalizedMediaRefs(mediaRefs);
        const structuredContent = options.structuredContent || false;
        const replayRequestId = options.replayRequestId || false;
        if (replayRequestId && !UUID_PATTERN.test(replayRequestId)) {
            return sendOutcome(options, false, true);
        }
        const sendContext = this.prepareSendContext(
            conversation,
            cleanBody,
            cleanMediaRefs,
            options.replyId,
            structuredContent,
            replayRequestId
        );
        if (!sendContext) {
            return sendOutcome(options, false, true);
        }
        const channelId = conversation.channel_id;
        const replyId = sendContext.replyId;
        const signature = `send-message:${channelId}:${
            replyId || ""
        }:${cleanBody}:${cleanMediaRefs.join(",")}${
            structuredContent ? `:card:${JSON.stringify(structuredContent)}` : ""
        }`;
        const requestId = replayRequestId || this.operationRequestId(signature);
        this.setChannelSending(channelId, true);
        let admissionConfirmed = false;
        try {
            const args = [
                channelId,
                cleanBody,
                replyId || false,
                requestId,
                cleanMediaRefs,
            ];
            if (structuredContent) {
                args.push(structuredContent);
            }
            const payload = await this.call("send_message", args);
            validateOperationEnvelope(payload, {
                channelId,
                requestId,
                requiredRecords: ["message"],
            });
            admissionConfirmed = true;
            this.completeOperationIntent(signature, requestId);
            if (!this.destroyed && this.state.selectedChannelId === channelId) {
                this.state.messages = mergeTimelineItems(
                    this.state.messages,
                    [payload.message],
                    {prepend: false}
                );
            }
            if (
                this.state.selectedChannelId === channelId &&
                replyMatches(this.state.replyTo, replyId)
            ) {
                this.state.replyTo = false;
            }
            if (!this.destroyed) {
                await this.refreshLoadedConversations({silent: true});
            }
            return sendOutcome(options, true, false, requestId);
        } catch (error) {
            if (admissionConfirmed) {
                // A refresh failure cannot undo the acknowledged server command.
                return sendOutcome(options, true, false, requestId);
            }
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Mensagem não enviada",
                sticky: true,
            });
            return sendOutcome(
                options,
                false,
                definitiveSendRejection(error),
                requestId
            );
        } finally {
            this.setChannelSending(channelId, false);
        }
    }

    actionRequestId(signature) {
        return this.operationRequestId(signature);
    }

    async runMessageAction(method, messageId, values, title) {
        const conversation = this.selectedConversation;
        const action = MESSAGE_ACTION_METHODS[method];
        const message = this.state.messages.find(
            (item) => item.message_id === messageId
        );
        if (
            !messageActionEnabled(conversation, message, action) ||
            !Number.isSafeInteger(messageId) ||
            messageId <= 0
        ) {
            return false;
        }
        const channelId = conversation.channel_id;
        const signature = `${method}:${channelId}:${messageId}:${JSON.stringify(
            values
        )}`;
        const requestId = this.actionRequestId(signature);
        try {
            const payload = await this.call(method, [
                channelId,
                messageId,
                ...values,
                requestId,
            ]);
            validateOperationEnvelope(payload, {
                channelId,
                requestId,
                requiredRecords:
                    method === "resend_message"
                        ? ["source_message", "message"]
                        : ["message"],
            });
            this.completeOperationIntent(signature, requestId);
            if (this.state.selectedChannelId === channelId) {
                const messageUpdates = [payload.source_message, payload.message].filter(
                    Boolean
                );
                if (messageUpdates.length) {
                    this.state.messages = mergeTimelineItems(
                        this.state.messages,
                        messageUpdates
                    );
                } else {
                    await this.refreshLatestTimeline();
                }
            }
            await this.refreshLoadedConversations({silent: true});
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title,
                sticky: true,
            });
            return false;
        }
    }

    reactMessage(messageId, emoji, operation = "add") {
        const cleanEmoji = typeof emoji === "string" ? emoji.trim() : "";
        if (!cleanEmoji || !["add", "remove"].includes(operation)) {
            return Promise.resolve(false);
        }
        return this.runMessageAction(
            "react_message",
            messageId,
            [cleanEmoji, operation],
            "Reação não enviada"
        );
    }

    editMessage(messageId, body) {
        const cleanBody = typeof body === "string" ? body.trim() : "";
        if (!cleanBody) {
            return Promise.resolve(false);
        }
        return this.runMessageAction(
            "edit_message",
            messageId,
            [cleanBody],
            "Mensagem não editada"
        );
    }

    deleteMessage(messageId) {
        return this.runMessageAction(
            "delete_message",
            messageId,
            [],
            "Mensagem não apagada"
        );
    }

    resendMessage(messageId) {
        return this.runMessageAction(
            "resend_message",
            messageId,
            [],
            _t("Mensagem não reenviada")
        );
    }

    reconcileSelectedConversation(normalizedItem) {
        if (!this.canViewAttribution(normalizedItem)) {
            this.resetAttribution(normalizedItem.channel_id);
        }
        if (
            this.state.contactLinker.open &&
            normalizedItem.identity &&
            normalizedItem.identity.partner
        ) {
            this.closeContactLinker();
        }
        const companyCapability =
            this.state.companyLinker.mode === "create"
                ? "create_company"
                : "link_company";
        const selectedPartner =
            normalizedItem.identity && normalizedItem.identity.partner;
        if (
            this.state.companyLinker.open &&
            (!selectedPartner ||
                selectedPartner.id !== this.state.companyLinker.partnerId ||
                !this.companyLinkingAvailable(companyCapability))
        ) {
            this.closeCompanyLinker();
        }
        this.reconcileReplySelection();
    }

    replaceConversation(item, {insert = false} = {}) {
        const known = isPlainRecord(item)
            ? this.knownPreference(item.channel_id)
            : null;
        const normalizedItem = this.resolvedPreference(
            normalizeConversationGroup(item)
        );
        if (
            !isRenderableConversation(normalizedItem) ||
            this.deletedConversationIds.has(normalizedItem.channel_id)
        ) {
            return false;
        }
        this.synchronizeOnMuteChange(known, normalizedItem);
        if (!this.conversationMatchesStructuralFilters(normalizedItem)) {
            this.state.conversations = this.state.conversations.filter(
                (conversation) => conversation.channel_id !== normalizedItem.channel_id
            );
            if (normalizedItem.channel_id === this.state.selectedChannelId) {
                this.clearConversationSelection({closePanes: true});
            }
            return true;
        }
        if (this.hiddenByPersonalFilters(normalizedItem)) {
            // Acknowledged, but a muted conversation that is not open never
            // comes back under "Sem silenciadas".
            this.state.conversations = this.state.conversations.filter(
                (conversation) => conversation.channel_id !== normalizedItem.channel_id
            );
            return true;
        }
        const index = this.state.conversations.findIndex(
            (conversation) =>
                isRenderableConversation(conversation) &&
                conversation.channel_id === normalizedItem.channel_id
        );
        if (index >= 0) {
            this.state.conversations.splice(index, 1, normalizedItem);
        } else if (
            insert ||
            normalizedItem.channel_id === this.state.selectedChannelId
        ) {
            this.state.conversations.unshift(normalizedItem);
        }
        // Otherwise a late answer for a conversation that left the list only
        // acknowledges: it never brings the row back by itself.
        if (
            normalizedItem.channel_id === this.state.selectedChannelId &&
            this.hasPeopleOrTagFilters &&
            !this.responsibilityVisibleConversations.some(
                (conversation) => conversation.channel_id === normalizedItem.channel_id
            )
        ) {
            this.clearConversationSelection({closePanes: true});
        } else if (normalizedItem.channel_id === this.state.selectedChannelId) {
            this.reconcileSelectedConversation(normalizedItem);
        }
        return true;
    }

    async applyConversationUpdate(payload, channelId) {
        validateEnvelope(payload);
        if (
            (payload.item && payload.item.channel_id !== channelId) ||
            (!payload.item && payload.removed_from_conversation !== true)
        ) {
            throw new TypeError("A conversa retornada pelo servidor é inválida.");
        }
        const targetIsSelected = this.state.selectedChannelId === channelId;
        if (payload.removed_from_conversation === true) {
            this.state.conversations = this.state.conversations.filter(
                (item) => item.channel_id !== channelId
            );
            if (targetIsSelected) {
                this.clearConversationSelection({closePanes: true});
            }
        } else if (!this.replaceConversation(payload.item)) {
            throw new TypeError("A conversa retornada pelo servidor é inválida.");
        }
        if (targetIsSelected && !this.state.selectedChannelId) {
            await this.loadConversations({reset: true});
        }
        return true;
    }

    async updateConversation(patch, channelId = this.state.selectedChannelId) {
        if (!this.loadedConversation(channelId)) {
            return false;
        }
        try {
            const payload = await this.call("update_conversation", [channelId, patch]);
            return await this.applyConversationUpdate(payload, channelId);
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Conversa não atualizada",
            });
            return false;
        }
    }

    async setConversationState(state, channelId = this.state.selectedChannelId) {
        return this.updateConversation({state}, channelId);
    }

    async resolveConversation(channelId, values) {
        const payload = await this.call("resolve_conversation", [
            channelId,
            values.reasonId,
            values.justification,
            values.requestId,
            values.revision,
        ]);
        if (!payload || !payload.item || payload.item.channel_id !== channelId) {
            throw new TypeError("A conversa retornada pelo servidor é inválida.");
        }
        return this.applyConversationUpdate(payload, channelId);
    }

    canManageConversation(channelId, capability) {
        const conversation = this.loadedConversation(channelId);
        return Boolean(
            conversation &&
                conversation.capabilities &&
                conversation.capabilities[capability] === true
        );
    }

    forgetDeletedConversation(channelId) {
        if (!Number.isSafeInteger(channelId) || channelId <= 0) {
            return false;
        }
        const wasLoaded = Boolean(this.loadedConversation(channelId));
        this.deletedConversationIds.add(channelId);
        // Deletion discards every list answer, a visible first page included;
        // it then shows the remaining rows as ready (tombstones filter them).
        this.listRequest += 1;
        this.state.conversations = this.state.conversations.filter(
            (item) => item.channel_id !== channelId
        );
        if (wasLoaded) {
            this.state.conversationTotal = Math.max(
                0,
                this.state.conversationTotal - 1
            );
        }
        if (this.preservedConversationChannelId === channelId) {
            this.preservedConversationChannelId = false;
        }
        if (this.state.selectedChannelId === channelId) {
            this.clearConversationSelection({closePanes: true});
        }
        this.state.listPhase = "ready";
        return true;
    }

    async deleteConversation(channelId) {
        if (!this.canManageConversation(channelId, "delete_conversation")) {
            return false;
        }
        try {
            const payload = await this.call("delete_conversation", [channelId]);
            validateEnvelope(payload);
            if (
                payload.channel_id !== channelId ||
                payload.removed_from_conversation !== true
            ) {
                throw new TypeError("O servidor não confirmou a exclusão da conversa.");
            }
            this.forgetDeletedConversation(channelId);
            this.scheduleSynchronization(false, false, {urgent: true});
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Conversa não excluída",
            });
            return false;
        }
    }

    async setConversationIgnored(ignored, channelId) {
        if (
            typeof ignored !== "boolean" ||
            !this.canManageConversation(channelId, "ignore_conversation")
        ) {
            return false;
        }
        try {
            const payload = await this.call("set_conversation_ignored", [
                channelId,
                ignored,
            ]);
            validateEnvelope(payload);
            if (this.deletedConversationIds.has(channelId)) {
                return false;
            }
            if (
                !payload.item ||
                payload.item.channel_id !== channelId ||
                payload.item.ignored !== ignored ||
                !this.replaceConversation(payload.item)
            ) {
                throw new TypeError("O servidor não confirmou a opção de ignorar.");
            }
            this.cancelListAnswers();
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Conversa não atualizada",
            });
            return false;
        }
    }

    applyConversationReadSnapshot(payload, channelId, filters) {
        validateEnvelope(payload);
        if (
            !isRenderableConversation(payload.item) ||
            payload.item.channel_id !== channelId ||
            !Number.isSafeInteger(payload.item.unread_count) ||
            payload.item.unread_count < 0
        ) {
            throw new TypeError("O servidor não confirmou o estado de leitura.");
        }
        this.acceptPreferenceSnapshot(payload.item);
        if (
            filters === JSON.stringify(this.conversationFilters()) &&
            this.loadedConversation(channelId)
        ) {
            this.replaceConversation(payload.item);
            if (
                this.state.filters.unreadOnly &&
                payload.item.unread_count === 0 &&
                channelId !== this.state.selectedChannelId
            ) {
                // A bounded refresh retains the loaded tail beyond 200 rows.
                // Remove the acknowledged row there too, while preserving the
                // currently open conversation like other silent list updates.
                this.state.conversations = this.state.conversations.filter(
                    (item) => item.channel_id !== channelId
                );
            }
            this.cancelListAnswers();
            if (
                channelId === this.state.timelineChannelId &&
                channelId === this.state.selectedChannelId
            ) {
                this.state.timelineFirstUnreadMessageId =
                    payload.item.first_unread_message_id || false;
            }
        }
    }

    /**
     * What a bulk read preparation depends on: the open conversation and the
     * filters of the list (L08-R05).
     *
     * @returns {String}
     */
    bulkReadGeneration() {
        return JSON.stringify([
            this.state.selectedChannelId || false,
            this.conversationFilters(),
        ]);
    }

    /**
     * Choose the unread conversations of the current list, except the open one.
     *
     * A preparation answered after the selection or the filters changed is
     * discarded and made again.
     *
     * @returns {Promise<Object|null>} {targets, count, remaining}, or null when
     *   the list kept changing
     */
    async prepareMarkAllRead() {
        for (let attempt = 0; attempt < 3 && !this.destroyed; attempt += 1) {
            const generation = this.bulkReadGeneration();
            const selected = this.state.selectedChannelId;
            const payload = await this.call("prepare_mark_conversations_read", [], {
                filters: this.conversationFilters(),
                exclude_channel_ids: selected ? [selected] : [],
            });
            validateEnvelope(payload);
            if (!validBulkReadPreparation(payload)) {
                throw new TypeError("O servidor não confirmou as conversas a ler.");
            }
            if (!this.destroyed && generation === this.bulkReadGeneration()) {
                return {
                    targets: payload.targets,
                    count: payload.targets.length,
                    remaining: payload.remaining === true,
                };
            }
        }
        return null;
    }

    /**
     * Mark the prepared conversations as read, agent side only.
     *
     * The conversation open at the confirmation stays out; the list stays
     * busy until the answer. Its projections are only immediate feedback, and
     * never for a conversation that changed during the request; the list is
     * then reloaded from the first page without the rows beyond the refresh
     * window, which no answer can keep coherent (L08-I05–I07).
     *
     * @param {Object} prepared the result of prepareMarkAllRead
     * @returns {Promise<Object|false>} {marked, remaining} or false
     */
    async markAllRead(prepared) {
        if (!prepared || this.state.bulkReadPending || this.destroyed) {
            return false;
        }
        const selected = this.state.selectedChannelId;
        const targets = prepared.targets.filter(
            (target) => target.channel_id !== selected
        );
        if (!targets.length) {
            return {marked: 0, remaining: prepared.remaining};
        }
        this.state.bulkReadPending = true;
        const before = new Map(
            targets.map((target) => [
                target.channel_id,
                this.snapshotGenerations(target.channel_id),
            ])
        );
        try {
            let payload = null;
            try {
                payload = await this.call("mark_conversations_read", [targets]);
            } finally {
                // Answered or not: the server may have applied the action even
                // when its answer was lost (L08-I08). Required until a refresh
                // is applied; a failed one leaves it to the next synchronization.
                this.listTailStale = true;
            }
            validateEnvelope(payload);
            if (
                !Number.isSafeInteger(payload.marked) ||
                !Array.isArray(payload.items)
            ) {
                throw new TypeError(
                    "O servidor não confirmou a leitura das conversas."
                );
            }
            for (const item of payload.items) {
                if (!this.changedSinceSnapshot(item, before)) {
                    this.applyBulkReadItem(item);
                }
            }
            // Started right away, with no wait in between: it discards every
            // list answer in flight, requested before the action.
            await this.refreshLoadedConversations({silent: true});
            return {marked: payload.marked, remaining: prepared.remaining};
        } catch (error) {
            // The outcome is unknown, and the server may even commit after this
            // reload (L08-I09): reload anyway, then report the failure.
            this.bulkReadUncertain = true;
            const reloaded = await this.refreshLoadedConversations({silent: true});
            throw Object.assign(
                new Error(error && error.message ? error.message : String(error)),
                {cause: error, bulkReadUncertain: true, listReloaded: reloaded === true}
            );
        } finally {
            this.state.bulkReadPending = false;
        }
    }

    snapshotGenerations(channelId) {
        return {
            changes: this.conversationGeneration(channelId),
            ownReads: this.ownReadGenerations.get(channelId) || 0,
        };
    }

    /**
     * Whether a conversation changed after its snapshot was requested.
     *
     * A read by this user elsewhere only lowers the unread count: a snapshot
     * with nothing unread stays true after it (L08-I06).
     *
     * @param {Object} item the snapshot
     * @param {Map} before the generations when it was requested, by id
     * @returns {Boolean}
     */
    changedSinceSnapshot(item, before) {
        if (!isPlainRecord(item) || !before.has(item.channel_id)) {
            return true;
        }
        const requested = before.get(item.channel_id);
        const current = this.snapshotGenerations(item.channel_id);
        return (
            current.changes !== requested.changes ||
            (current.ownReads !== requested.ownReads && item.unread_count !== 0)
        );
    }

    applyBulkReadItem(item) {
        if (
            !isRenderableConversation(item) ||
            !Number.isSafeInteger(item.unread_count) ||
            item.unread_count < 0
        ) {
            return false;
        }
        this.acceptPreferenceSnapshot(item);
        if (!this.loadedConversation(item.channel_id)) {
            return false;
        }
        this.replaceConversation(item);
        if (
            this.state.filters.unreadOnly &&
            item.unread_count === 0 &&
            item.channel_id !== this.state.selectedChannelId
        ) {
            this.state.conversations = this.state.conversations.filter(
                (row) => row.channel_id !== item.channel_id
            );
        }
        return true;
    }

    async markConversationRead(channelId) {
        const conversation = this.loadedConversation(channelId);
        if (
            !conversation ||
            this.destroyed ||
            this.suspendedSeenChannels.has(channelId)
        ) {
            return false;
        }
        // The explicit row command acknowledges the server-provided preview
        // visible at the click, never an unseen message arriving during the RPC.
        const messageId =
            conversation.last_message && conversation.last_message.message_id;
        if (!Number.isSafeInteger(messageId) || messageId <= 0) {
            this.notify("Atualize a lista de conversas e tente novamente.", {
                type: "warning",
                title: "Última mensagem indisponível",
            });
            return false;
        }
        const filters = JSON.stringify(this.conversationFilters());
        const current = () =>
            !this.destroyed && !this.deletedConversationIds.has(channelId);
        this.suspendedSeenChannels.add(channelId);
        if (channelId === this.state.selectedChannelId) {
            this.cancelSeenRetry();
        }
        try {
            await Promise.allSettled(
                [...(this.pendingSeenRequests.get(channelId) || [])].map(
                    (request) => request.promise
                )
            );
            if (!current()) {
                return false;
            }
            const seen = await this.call("mark_seen", [channelId, messageId]);
            if (
                !seen ||
                seen.channel_id !== channelId ||
                seen.message_id !== messageId
            ) {
                throw new TypeError("O servidor não confirmou a leitura da mensagem.");
            }
            if (!current()) {
                return false;
            }
            const payload = await this.call("get_conversation", [channelId]);
            if (!current()) {
                return false;
            }
            this.applyConversationReadSnapshot(payload, channelId, filters);
            // Refresh the current filters (including Unread) and totals instead
            // of blindly zeroing a counter that may already contain new work.
            await this.refreshLoadedConversations({silent: true});
            return true;
        } catch (error) {
            if (current()) {
                this.scheduleSynchronization(false, false, {urgent: true});
                this.notify(errorMessage(error), {
                    type: "danger",
                    title: "Não foi possível confirmar a leitura",
                });
            }
            return false;
        } finally {
            this.suspendedSeenChannels.delete(channelId);
        }
    }

    async getRetentionPolicy(channelId, preview = false) {
        if (!this.loadedConversation(channelId) || this.destroyed) {
            throw new Error("A conversa não está disponível.");
        }
        const payload = await this.call("get_retention_policy", [channelId], {preview});
        return validateRetentionResponse(payload, channelId, preview);
    }

    async setRetentionPreserve(channelId, preserve, confirmationToken = false) {
        const conversation = this.loadedConversation(channelId);
        if (!conversation || this.destroyed || typeof preserve !== "boolean") {
            throw new Error("A conversa não está disponível.");
        }
        const payload = await this.call(
            "set_retention_preserve",
            [channelId, preserve],
            {
                confirmation_token: confirmationToken,
            }
        );
        validateRetentionResponse(payload, channelId);
        if (payload.policy.preserve !== preserve) {
            throw new TypeError("O servidor não confirmou a preservação do grupo.");
        }
        const current = this.loadedConversation(channelId);
        if (!this.destroyed && current) {
            current.retention = payload.policy;
            this.scheduleSynchronization(false, false, {urgent: true});
        }
        return payload;
    }

    async markConversationUnread(channelId) {
        if (
            !this.loadedConversation(channelId) ||
            this.suspendedSeenChannels.has(channelId)
        ) {
            return false;
        }
        this.suspendedSeenChannels.add(channelId);
        if (channelId === this.state.selectedChannelId) {
            this.cancelSeenRetry();
        }
        try {
            // Cancelling the UI token cannot undo a mark_seen already at the
            // server. Drain those writes before moving the personal pointer.
            await Promise.allSettled(
                [...(this.pendingSeenRequests.get(channelId) || [])].map(
                    (request) => request.promise
                )
            );
            if (this.destroyed || this.deletedConversationIds.has(channelId)) {
                return false;
            }
            const payload = await this.call("mark_conversation_unread", [channelId]);
            validateEnvelope(payload);
            if (this.deletedConversationIds.has(channelId)) {
                return false;
            }
            if (
                !payload.item ||
                payload.item.channel_id !== channelId ||
                !this.replaceConversation(payload.item)
            ) {
                throw new TypeError("O servidor não confirmou a conversa não lida.");
            }
            this.cancelListAnswers();
            if (channelId === this.state.selectedChannelId) {
                this.clearConversationSelection({closePanes: true});
            }
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Conversa não marcada como não lida",
            });
            return false;
        } finally {
            this.suspendedSeenChannels.delete(channelId);
        }
    }

    async setConversationPreference(patch, channelId = this.state.selectedChannelId) {
        if (
            !this.loadedConversation(channelId) ||
            !isPlainRecord(patch) ||
            !Object.keys(patch).length ||
            Object.keys(patch).some(
                (key) =>
                    !["pinned", "muted"].includes(key) ||
                    typeof patch[key] !== "boolean"
            )
        ) {
            return false;
        }
        try {
            const payload = await this.call("set_conversation_preference", [
                channelId,
                patch,
            ]);
            validateEnvelope(payload);
            if (
                !payload.item ||
                payload.item.channel_id !== channelId ||
                !this.replaceConversation(payload.item)
            ) {
                throw new TypeError(
                    "A preferência retornada pelo servidor é inválida."
                );
            }
            await this.refreshLoadedConversations({silent: true});
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Preferência não atualizada",
            });
            return false;
        }
    }

    toggleConversationPinned(channelId = this.state.selectedChannelId) {
        const preference = conversationPreference(this.loadedConversation(channelId));
        return this.setConversationPreference({pinned: !preference.pinned}, channelId);
    }

    toggleConversationMuted(channelId = this.state.selectedChannelId) {
        const preference = conversationPreference(this.loadedConversation(channelId));
        return this.setConversationPreference({muted: !preference.muted}, channelId);
    }

    setResponsible(responsibleId) {
        if (this.capabilities.manage_assignment !== true) {
            return Promise.resolve(false);
        }
        return this.updateConversation({
            responsible_id: responsibleId ? Number(responsibleId) : false,
        });
    }

    toggleTag(tagId) {
        const conversation = this.selectedConversation;
        if (!conversation) {
            return Promise.resolve(false);
        }
        const selected = new Set((conversation.tags || []).map((tag) => tag.id));
        if (selected.has(tagId)) {
            selected.delete(tagId);
        } else {
            selected.add(tagId);
        }
        return this.updateConversation({tag_ids: [...selected]});
    }

    async claimConversation() {
        const channelId = this.state.selectedChannelId;
        if (!channelId) {
            return false;
        }
        try {
            const payload = await this.call("claim_conversation", [channelId]);
            validateEnvelope(payload);
            this.replaceConversation(payload.item);
            if (
                this.state.filters.responsibility !== "all" &&
                !this.state.selectedChannelId
            ) {
                await this.loadConversations({reset: true});
            }
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Conversa não assumida",
            });
            return false;
        }
    }

    identityLinkingAvailable(targetKind = "person", mode = "search") {
        if (
            !IDENTITY_LINK_TARGETS.has(targetKind) ||
            !["search", "create"].includes(mode)
        ) {
            return false;
        }
        const conversation = this.selectedConversation;
        const identity = conversation && conversation.identity;
        if (!identity || identity.partner) {
            return false;
        }
        const capability =
            targetKind === "central_company"
                ? mode === "create"
                    ? "create_central_company"
                    : "link_central_company"
                : mode === "create"
                ? "create_contact"
                : "link_contact";
        return this.capabilities[capability] === true;
    }

    openContactLinker(mode = "search", targetKind = "person") {
        if (!this.identityLinkingAvailable(targetKind, mode)) {
            return false;
        }
        if (
            this.state.contactLinker.open &&
            this.state.contactLinker.channelId === this.state.selectedChannelId &&
            this.state.contactLinker.targetKind === targetKind &&
            this.state.contactLinker.mode === mode
        ) {
            return false;
        }
        this.closeContactLinker();
        this.state.contactLinker.open = true;
        this.state.contactLinker.channelId = this.state.selectedChannelId;
        this.state.contactLinker.targetKind = targetKind;
        this.state.contactLinker.mode = mode;
        return true;
    }

    closeContactLinker() {
        if (this.contactSearchTimer !== null) {
            this.contactTimer.clearTimeout(this.contactSearchTimer);
            this.contactSearchTimer = null;
        }
        this.contactLinkerRequest += 1;
        this.state.contactLinker.open = false;
        this.state.contactLinker.channelId = false;
        this.state.contactLinker.targetKind = "person";
        this.state.contactLinker.mode = "search";
        this.state.contactLinker.phase = "idle";
        this.state.contactLinker.query = "";
        this.state.contactLinker.results = [];
        this.state.contactLinker.error = "";
    }

    isCurrentContactLinker(request, channelId, targetKind) {
        const conversation = this.selectedConversation;
        const identity = conversation && conversation.identity;
        return Boolean(
            !this.destroyed &&
                request === this.contactLinkerRequest &&
                this.state.contactLinker.open &&
                this.state.contactLinker.channelId === channelId &&
                this.state.contactLinker.targetKind === targetKind &&
                this.state.selectedChannelId === channelId &&
                identity &&
                !identity.partner
        );
    }

    companyLinkingAvailable(capability = "link_company") {
        const conversation = this.selectedConversation;
        const identity = conversation && conversation.identity;
        const partner = identity && identity.partner;
        return Boolean(
            this.capabilities.link_company === true &&
                this.capabilities[capability] === true &&
                partner &&
                partner.is_company === false &&
                ((partner.company_linking_allowed === true &&
                    partner.company === false) ||
                    (partner.company &&
                        partner.secondary_company_linking_allowed === true))
        );
    }

    companyOperationKey(channelId, partnerId) {
        return `${channelId}:${partnerId}`;
    }

    companyOperationPending(channelId, partnerId) {
        return Boolean(
            Number.isSafeInteger(this.state.companyOperationsRevision) &&
                this.pendingCompanyOperations.has(
                    this.companyOperationKey(channelId, partnerId)
                )
        );
    }

    openCompanyLinker(mode = "search") {
        const capability = mode === "create" ? "create_company" : "link_company";
        if (
            !["search", "create"].includes(mode) ||
            !this.companyLinkingAvailable(capability)
        ) {
            return false;
        }
        const partner = this.selectedConversation.identity.partner;
        if (this.companyOperationPending(this.state.selectedChannelId, partner.id)) {
            return false;
        }
        if (
            this.state.companyLinker.open &&
            this.state.companyLinker.channelId === this.state.selectedChannelId &&
            this.state.companyLinker.partnerId === partner.id &&
            this.state.companyLinker.mode === mode
        ) {
            return false;
        }
        this.closeCompanyLinker();
        this.state.companyLinker.open = true;
        this.state.companyLinker.channelId = this.state.selectedChannelId;
        this.state.companyLinker.partnerId = partner.id;
        this.state.companyLinker.mode = mode;
        this.state.companyLinker.relationKind = partnerCompanyForIdentity(
            this.selectedConversation.identity
        )
            ? "secondary"
            : "primary";
        return true;
    }

    closeCompanyLinker() {
        if (this.companySearchTimer !== null) {
            this.companyTimer.clearTimeout(this.companySearchTimer);
            this.companySearchTimer = null;
        }
        this.companyLinkerRequest += 1;
        this.state.companyLinker.open = false;
        this.state.companyLinker.channelId = false;
        this.state.companyLinker.partnerId = false;
        this.state.companyLinker.mode = "search";
        this.state.companyLinker.relationKind = "primary";
        this.state.companyLinker.phase = "idle";
        this.state.companyLinker.query = "";
        this.state.companyLinker.results = [];
        this.state.companyLinker.error = "";
    }

    isCurrentCompanyLinker(request, channelId) {
        const conversation = this.selectedConversation;
        const partner =
            conversation && conversation.identity && conversation.identity.partner;
        return Boolean(
            !this.destroyed &&
                request === this.companyLinkerRequest &&
                this.state.companyLinker.open &&
                this.state.companyLinker.channelId === channelId &&
                this.state.selectedChannelId === channelId &&
                partner &&
                partner.id === this.state.companyLinker.partnerId
        );
    }

    setCompanySearch(query) {
        const linker = this.state.companyLinker;
        if (
            !linker.open ||
            linker.mode !== "search" ||
            linker.channelId !== this.state.selectedChannelId ||
            linker.phase === "saving"
        ) {
            return false;
        }
        const searchQuery = typeof query === "string" ? query : "";
        linker.query = searchQuery;
        linker.error = "";
        if (this.companySearchTimer !== null) {
            this.companyTimer.clearTimeout(this.companySearchTimer);
            this.companySearchTimer = null;
        }
        const request = ++this.companyLinkerRequest;
        const channelId = linker.channelId;
        const partnerId = linker.partnerId;
        const relationKind = linker.relationKind;
        linker.results = [];
        if (searchQuery.trim().length < 2) {
            linker.phase = "idle";
            return true;
        }
        linker.phase = "loading";
        this.companySearchTimer = this.companyTimer.setTimeout(async () => {
            this.companySearchTimer = null;
            try {
                const payload = await this.call("search_partner_companies", [
                    channelId,
                    partnerId,
                    searchQuery,
                    ...(relationKind === "secondary" ? [12, "secondary"] : []),
                ]);
                validateEnvelope(payload);
                if (!this.isCurrentCompanyLinker(request, channelId)) {
                    return;
                }
                linker.results = Array.isArray(payload.items)
                    ? payload.items.map(normalizePartnerCompany).filter(Boolean)
                    : [];
                linker.phase = "ready";
            } catch (error) {
                if (!this.isCurrentCompanyLinker(request, channelId)) {
                    return;
                }
                linker.phase = "error";
                linker.error = errorMessage(error);
                this.notify(linker.error, {
                    type: "danger",
                    title: "Empresas indisponíveis",
                });
            }
        }, 260);
        return true;
    }

    beginCompanySave(capability) {
        const conversation = this.selectedConversation;
        const partner =
            conversation && conversation.identity && conversation.identity.partner;
        if (
            this.state.companyLinker.phase === "saving" ||
            this.state.companyLinker.channelId !== this.state.selectedChannelId ||
            !partner ||
            this.state.companyLinker.partnerId !== partner.id ||
            this.companyOperationPending(
                this.state.companyLinker.channelId,
                this.state.companyLinker.partnerId
            ) ||
            !this.companyLinkingAvailable(capability)
        ) {
            return false;
        }
        if (this.companySearchTimer !== null) {
            this.companyTimer.clearTimeout(this.companySearchTimer);
            this.companySearchTimer = null;
        }
        const request = ++this.companyLinkerRequest;
        const operationKey = this.companyOperationKey(
            this.state.companyLinker.channelId,
            this.state.companyLinker.partnerId
        );
        this.pendingCompanyOperations.add(operationKey);
        this.state.companyOperationsRevision += 1;
        this.state.companyLinker.phase = "saving";
        this.state.companyLinker.error = "";
        return {
            request,
            channelId: this.state.companyLinker.channelId,
            partnerId: this.state.companyLinker.partnerId,
            relationKind: this.state.companyLinker.relationKind,
            operationKey,
        };
    }

    setContactSearch(query) {
        const linker = this.state.contactLinker;
        if (
            !linker.open ||
            linker.mode !== "search" ||
            linker.channelId !== this.state.selectedChannelId ||
            linker.phase === "saving"
        ) {
            return false;
        }
        const searchQuery = typeof query === "string" ? query : "";
        linker.query = searchQuery;
        linker.error = "";
        if (this.contactSearchTimer !== null) {
            this.contactTimer.clearTimeout(this.contactSearchTimer);
            this.contactSearchTimer = null;
        }
        const request = ++this.contactLinkerRequest;
        const channelId = linker.channelId;
        const targetKind = linker.targetKind;
        linker.results = [];
        if (searchQuery.trim().length < 2) {
            linker.phase = "idle";
            return true;
        }
        linker.phase = "loading";
        this.contactSearchTimer = this.contactTimer.setTimeout(async () => {
            this.contactSearchTimer = null;
            try {
                const method =
                    targetKind === "central_company"
                        ? "search_central_companies"
                        : "search_partners";
                const payload = await this.call(method, [channelId, searchQuery]);
                validateEnvelope(payload);
                if (!this.isCurrentContactLinker(request, channelId, targetKind)) {
                    return;
                }
                linker.results = Array.isArray(payload.items)
                    ? payload.items.map(normalizePartnerCompany).filter(Boolean)
                    : [];
                linker.phase = "ready";
            } catch (error) {
                if (!this.isCurrentContactLinker(request, channelId, targetKind)) {
                    return;
                }
                linker.phase = "error";
                linker.error = errorMessage(error);
                this.notify(linker.error, {
                    type: "danger",
                    title:
                        targetKind === "central_company"
                            ? "Empresas indisponíveis"
                            : "Pessoas indisponíveis",
                });
            }
        }, 260);
        return true;
    }

    beginContactSave(targetKind, mode) {
        const linker = this.state.contactLinker;
        if (
            linker.phase === "saving" ||
            linker.channelId !== this.state.selectedChannelId ||
            linker.targetKind !== targetKind ||
            linker.mode !== mode ||
            !this.identityLinkingAvailable(targetKind, mode)
        ) {
            return false;
        }
        if (this.contactSearchTimer !== null) {
            this.contactTimer.clearTimeout(this.contactSearchTimer);
            this.contactSearchTimer = null;
        }
        const request = ++this.contactLinkerRequest;
        linker.phase = "saving";
        linker.error = "";
        return {
            request,
            channelId: linker.channelId,
            targetKind,
        };
    }

    applyIdentity(channelId, identity) {
        const conversation = this.state.conversations.find(
            (item) => item.channel_id === channelId
        );
        if (!conversation || !identity) {
            return false;
        }
        this.replaceConversation({
            ...conversation,
            identity,
            name: identity.name || conversation.name,
        });
        return true;
    }

    applyIdentityForPartner(channelId, partnerId, identity) {
        const conversation = this.state.conversations.find(
            (item) => item.channel_id === channelId
        );
        const currentPartner =
            conversation && conversation.identity && conversation.identity.partner;
        if (!currentPartner || currentPartner.id !== partnerId) {
            return false;
        }
        return this.applyIdentity(channelId, identity);
    }

    async refreshSelectedConversation({silent = false, automatic = false} = {}) {
        const channelId = this.state.selectedChannelId;
        if (!channelId) {
            return false;
        }
        try {
            const payload = await this.call(
                "get_conversation",
                [channelId],
                undefined,
                {silent, automatic}
            );
            validateEnvelope(payload);
            if (this.destroyed) {
                return false;
            }
            if (channelId === this.state.selectedChannelId) {
                this.reconcileTagCatalog(payload.item && payload.item.tags);
                this.replaceConversation(payload.item);
                if (!this.state.selectedChannelId) {
                    await this.loadConversations({
                        reset: true,
                        ...(automatic ? {silent: true, automatic: true} : {}),
                    });
                }
            } else if (payload.item && payload.item.channel_id === channelId) {
                // No longer open: only its preference still counts.
                this.acceptPreferenceSnapshot(payload.item);
            }
            return true;
        } catch (error) {
            this.syncCycleFailed = this.syncCycleFailed || automatic;
            if (this.destroyed) {
                return false;
            }
            if (accessWasRevoked(error)) {
                this.revokeConversation(channelId);
                return false;
            }
            if (!silent) {
                this.notify(errorMessage(error), {
                    type: "danger",
                    title: "Conversa não atualizada",
                });
            }
            return false;
        }
    }

    revokeConversation(channelId) {
        if (
            this.restoringChannelId === channelId &&
            this.state.selectedChannelId === channelId
        ) {
            // Still being restored: it leaves silently, memory included.
            this.forgetRefusedRestoration(channelId);
            return;
        }
        this.state.conversations = this.state.conversations.filter(
            (item) => item.channel_id !== channelId
        );
        if (this.state.selectedChannelId !== channelId) {
            return;
        }
        this.timelineRequest += 1;
        this.resetTimelineContinuity();
        this.resetAttribution();
        this.resetProductivity();
        this.resetQuickReplies();
        this.state.selectedChannelId = false;
        this.state.seenPausedChannelId = false;
        this.state.timelineChannelId = false;
        this.state.messages = [];
        this.state.timelinePhase = "idle";
        this.state.timelineHasMore = false;
        this.state.nextBeforeMessageId = false;
        this.state.replyTo = false;
        this.state.detailsOpen = false;
        this.state.mobilePane = "list";
        this.closeContactLinker();
        this.closeCompanyLinker();
        this.notify("Seu acesso a esta conversa foi removido.", {
            type: "warning",
            title: "Conversa atualizada",
        });
    }

    async linkPartner(partnerId) {
        if (!Number.isSafeInteger(partnerId) || partnerId <= 0) {
            return false;
        }
        const operation = this.beginContactSave("person", "search");
        if (!operation) {
            return false;
        }
        try {
            const payload = await this.call("link_partner", [
                operation.channelId,
                partnerId,
            ]);
            validateEnvelope(payload);
            const identity = identityMutationIdentity(
                payload,
                "person",
                false,
                partnerId
            );
            this.applyIdentity(operation.channelId, identity);
            if (
                this.isCurrentContactLinker(
                    operation.request,
                    operation.channelId,
                    operation.targetKind
                )
            ) {
                this.closeContactLinker();
            }
            this.notify("Pessoa vinculada a este perfil.", {type: "success"});
            return true;
        } catch (error) {
            if (
                this.isCurrentContactLinker(
                    operation.request,
                    operation.channelId,
                    operation.targetKind
                )
            ) {
                this.state.contactLinker.phase = "error";
                this.state.contactLinker.error = errorMessage(error);
            }
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Pessoa não vinculada",
            });
            return false;
        }
    }

    async createAndLinkPartner(values) {
        const operation = this.beginContactSave("person", "create");
        if (!operation) {
            return false;
        }
        try {
            const payload = await this.call("create_and_link_partner", [
                operation.channelId,
                values,
            ]);
            validateEnvelope(payload);
            const identity = identityMutationIdentity(payload, "person", false);
            this.applyIdentity(operation.channelId, identity);
            if (
                this.isCurrentContactLinker(
                    operation.request,
                    operation.channelId,
                    operation.targetKind
                )
            ) {
                this.closeContactLinker();
            }
            this.notify(
                payload.created === false
                    ? "Este perfil já estava vinculado a uma pessoa."
                    : "Pessoa criada e vinculada a este perfil.",
                {type: payload.created === false ? "info" : "success"}
            );
            return true;
        } catch (error) {
            if (
                this.isCurrentContactLinker(
                    operation.request,
                    operation.channelId,
                    operation.targetKind
                )
            ) {
                this.state.contactLinker.phase = "error";
                this.state.contactLinker.error = errorMessage(error);
            }
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Pessoa não criada",
            });
            return false;
        }
    }

    async linkCentralCompany(companyPartnerId) {
        if (!Number.isSafeInteger(companyPartnerId) || companyPartnerId <= 0) {
            return false;
        }
        const operation = this.beginContactSave("central_company", "search");
        if (!operation) {
            return false;
        }
        try {
            const payload = await this.call("link_central_company", [
                operation.channelId,
                companyPartnerId,
            ]);
            validateEnvelope(payload);
            const identity = identityMutationIdentity(
                payload,
                "central_company",
                true,
                companyPartnerId
            );
            this.applyIdentity(operation.channelId, identity);
            if (
                this.isCurrentContactLinker(
                    operation.request,
                    operation.channelId,
                    operation.targetKind
                )
            ) {
                this.closeContactLinker();
            }
            this.notify("Empresa central vinculada a este perfil.", {type: "success"});
            return true;
        } catch (error) {
            if (
                this.isCurrentContactLinker(
                    operation.request,
                    operation.channelId,
                    operation.targetKind
                )
            ) {
                this.state.contactLinker.phase = "error";
                this.state.contactLinker.error = errorMessage(error);
            }
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Empresa central não vinculada",
            });
            return false;
        }
    }

    async createAndLinkCentralCompany(values) {
        const operation = this.beginContactSave("central_company", "create");
        if (!operation) {
            return false;
        }
        try {
            const payload = await this.call("create_and_link_central_company", [
                operation.channelId,
                values,
            ]);
            validateEnvelope(payload);
            const identity = identityMutationIdentity(payload, "central_company", true);
            this.applyIdentity(operation.channelId, identity);
            if (
                this.isCurrentContactLinker(
                    operation.request,
                    operation.channelId,
                    operation.targetKind
                )
            ) {
                this.closeContactLinker();
            }
            this.notify(
                payload.created === false
                    ? "Este perfil já estava vinculado a uma empresa central."
                    : "Empresa central criada e vinculada a este perfil.",
                {type: payload.created === false ? "info" : "success"}
            );
            return true;
        } catch (error) {
            if (
                this.isCurrentContactLinker(
                    operation.request,
                    operation.channelId,
                    operation.targetKind
                )
            ) {
                this.state.contactLinker.phase = "error";
                this.state.contactLinker.error = errorMessage(error);
            }
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Empresa central não criada",
            });
            return false;
        }
    }

    async linkPartnerCompany(companyPartnerId) {
        if (!Number.isSafeInteger(companyPartnerId) || companyPartnerId <= 0) {
            return false;
        }
        const operation = this.beginCompanySave("link_company");
        if (!operation) {
            return false;
        }
        try {
            const payload = await this.call("link_partner_company", [
                operation.channelId,
                operation.partnerId,
                companyPartnerId,
                ...(operation.relationKind === "secondary" ? ["secondary"] : []),
            ]);
            validateEnvelope(payload);
            if (this.destroyed) {
                return false;
            }
            const identity = companyMutationIdentity(
                payload,
                operation.partnerId,
                companyPartnerId,
                operation.relationKind
            );
            const applied = this.applyIdentityForPartner(
                operation.channelId,
                operation.partnerId,
                identity
            );
            if (!applied) {
                return true;
            }
            if (this.isCurrentCompanyLinker(operation.request, operation.channelId)) {
                this.closeCompanyLinker();
            }
            this.notify("Empresa vinculada ao contato.", {type: "success"});
            return true;
        } catch (error) {
            if (this.isCurrentCompanyLinker(operation.request, operation.channelId)) {
                this.state.companyLinker.phase = "error";
                this.state.companyLinker.error = errorMessage(error);
            }
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Empresa não vinculada",
            });
            return false;
        } finally {
            if (this.pendingCompanyOperations.delete(operation.operationKey)) {
                if (!this.destroyed) {
                    this.state.companyOperationsRevision += 1;
                }
            }
        }
    }

    async createAndLinkPartnerCompany(values) {
        const operation = this.beginCompanySave("create_company");
        if (!operation) {
            return false;
        }
        try {
            const payload = await this.call("create_and_link_partner_company", [
                operation.channelId,
                operation.partnerId,
                values,
                ...(operation.relationKind === "secondary" ? ["secondary"] : []),
            ]);
            validateEnvelope(payload);
            if (this.destroyed) {
                return false;
            }
            const identity = companyMutationIdentity(
                payload,
                operation.partnerId,
                false,
                operation.relationKind
            );
            const applied = this.applyIdentityForPartner(
                operation.channelId,
                operation.partnerId,
                identity
            );
            if (!applied) {
                return true;
            }
            if (this.isCurrentCompanyLinker(operation.request, operation.channelId)) {
                this.closeCompanyLinker();
            }
            this.notify("Empresa criada e vinculada ao contato.", {type: "success"});
            return true;
        } catch (error) {
            if (this.isCurrentCompanyLinker(operation.request, operation.channelId)) {
                this.state.companyLinker.phase = "error";
                this.state.companyLinker.error = errorMessage(error);
            }
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Empresa não criada",
            });
            return false;
        } finally {
            if (this.pendingCompanyOperations.delete(operation.operationKey)) {
                if (!this.destroyed) {
                    this.state.companyOperationsRevision += 1;
                }
            }
        }
    }

    async unlinkPartnerCompany(companyId, relationKind = "primary") {
        const channelId = this.state.selectedChannelId;
        const identity =
            this.selectedConversation && this.selectedConversation.identity;
        const partner = identity && identity.partner;
        const linked = companyRelationshipExists(identity, companyId, relationKind);
        if (
            !partner ||
            !linked ||
            !partner.company_management_allowed ||
            !this.capabilities.link_company ||
            this.companyOperationPending(channelId, partner.id)
        ) {
            return false;
        }
        const operationKey = this.companyOperationKey(channelId, partner.id);
        this.pendingCompanyOperations.add(operationKey);
        this.state.companyOperationsRevision += 1;
        try {
            const payload = await this.call("unlink_partner_company", [
                channelId,
                partner.id,
                companyId,
                relationKind,
            ]);
            validateEnvelope(payload);
            if (this.destroyed) {
                return false;
            }
            const updated = companyUnlinkIdentity(
                payload,
                partner.id,
                companyId,
                relationKind
            );
            this.applyIdentityForPartner(channelId, partner.id, updated);
            if (
                this.state.companyLinker.channelId === channelId &&
                this.state.companyLinker.partnerId === partner.id
            ) {
                this.closeCompanyLinker();
            }
            this.notify("Empresa desvinculada. O contato foi mantido.", {
                type: "success",
            });
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {type: "danger"});
            return false;
        } finally {
            this.pendingCompanyOperations.delete(operationKey);
            if (!this.destroyed) {
                this.state.companyOperationsRevision += 1;
            }
        }
    }

    async unlinkPartner() {
        const channelId = this.state.selectedChannelId;
        const conversation = this.selectedConversation;
        const partner =
            conversation && conversation.identity && conversation.identity.partner;
        if (
            !partner ||
            !Number.isSafeInteger(partner.id) ||
            partner.id <= 0 ||
            this.companyOperationPending(channelId, partner.id)
        ) {
            return false;
        }
        const expectedPartnerId = partner.id;
        try {
            const payload = await this.call("unlink_partner", [
                channelId,
                expectedPartnerId,
            ]);
            validateEnvelope(payload);
            if (
                !isPlainRecord(payload.identity) ||
                payload.identity.partner !== false
            ) {
                throw new Error("O servidor não confirmou o desvínculo do cadastro.");
            }
            const applied = this.applyIdentityForPartner(
                channelId,
                expectedPartnerId,
                payload.identity
            );
            if (!applied) {
                return true;
            }
            this.closeCompanyLinker();
            this.notify("O perfil voltou a usar somente o convidado original.", {
                type: "success",
            });
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {type: "danger"});
            return false;
        }
    }

    async renameGuest(name) {
        const channelId = this.state.selectedChannelId;
        const cleanName = typeof name === "string" ? name.trim() : "";
        if (!channelId || !cleanName || !this.capabilities.rename_guest) {
            return false;
        }
        try {
            const payload = await this.call("rename_guest", [channelId, cleanName]);
            validateEnvelope(payload);
            this.applyIdentity(channelId, payload.identity);
            this.notify("Nome do perfil convidado atualizado.", {type: "success"});
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Nome não atualizado",
            });
            return false;
        }
    }

    showConversationList() {
        this.state.mobilePane = "list";
    }

    showConversation() {
        this.state.mobilePane = "conversation";
    }

    toggleDetails() {
        this.state.detailsOpen = !this.state.detailsOpen;
        this.inboxLayout.detailsOpen = this.state.detailsOpen;
        this.scheduleInboxPreferencesSave();
        if (
            this.state.detailsOpen &&
            this.state.selectedChannelId &&
            this.canViewAttribution()
        ) {
            this.loadAttribution({silent: true});
        }
    }

    onConnect() {
        this.state.realtime = "online";
        this.startConsistencySynchronization();
    }

    onDisconnect() {
        this.state.realtime = "offline";
        this.startConsistencySynchronization();
    }

    onReconnect() {
        this.state.realtime = "online";
        this.startConsistencySynchronization();
        this.scheduleSynchronization(true, true);
        this.scheduleConnectionHealthRefresh();
    }

    onReconnecting() {
        this.state.realtime = "connecting";
        this.startConsistencySynchronization();
    }

    /**
     * Consume the data-free attribution invalidation event.
     *
     * @param {Object} payload normalized Contact Center bus event
     * @returns {Boolean} whether the event was handled
     */
    handleAttributionNotification(payload) {
        if (payload.event_type !== "attribution_updated") {
            return false;
        }
        if (
            payload.channel_id === this.state.selectedChannelId &&
            this.state.detailsOpen &&
            this.canViewAttribution()
        ) {
            this.loadAttribution({silent: true, preserve: true});
        }
        return true;
    }

    handleProductivityNotification(payload) {
        if (payload.event_type !== "productivity_updated") {
            return false;
        }
        if (
            payload.channel_id === this.state.selectedChannelId &&
            this.state.productivity.channelId === payload.channel_id &&
            this.state.productivity.phase !== "idle"
        ) {
            this.loadProductivity({force: true, notify: false});
        }
        // Productivity changes also affect the conversation list (follow-up
        // filters and badges). This handler is terminal in onNotification, so
        // it must schedule the projection refresh itself.
        this.scheduleSynchronization(false, false);
        return true;
    }

    handleAttentionNotification(payload) {
        if (
            this.attention &&
            payload.personal_attention !== false &&
            !this.newerMuteThanEvent(payload)
        ) {
            this.attention.receive(payload);
        }
    }

    /**
     * Whether this tab knows a mute newer than the one an event was sent with.
     *
     * The server decides personal_attention with the recipient's preference
     * at publication and sends its revision: only a mute of a higher revision,
     * loaded or remembered (a row that left the list), overrides it
     * (L07-R14-01); an older remembered mute never silences a conversation
     * unmuted since (R15-01). An event without the revision keeps the loaded
     * row's mute.
     *
     * @param {Object} payload the inbound message event
     * @returns {Boolean}
     */
    newerMuteThanEvent(payload) {
        const known = conversationPreference({
            preference: this.knownPreference(payload.channel_id),
        });
        if (!known.muted) {
            return false;
        }
        return (
            !Number.isSafeInteger(payload.preference_revision) ||
            known.revision > payload.preference_revision
        );
    }

    handleDeliveryNotification(payload) {
        if (
            payload.event_type !== "delivery_updated" ||
            payload.channel_id !== this.state.selectedChannelId ||
            payload.refresh === true
        ) {
            return false;
        }
        this.state.messages = this.state.messages.map((message) => {
            if (message.message_id !== payload.message_id) {
                return message;
            }
            const dispatchState = payload.dispatch_state || message.dispatch_state;
            return {
                ...message,
                delivery_state: payload.state || message.delivery_state,
                dispatch_state: dispatchState,
                // A prior ambiguous/failure reason belongs to the old dispatch
                // projection. Exact provider evidence closes that projection and
                // must remove its warning immediately, without a timeline reload.
                dispatch_reason:
                    dispatchState === "done" ? "" : message.dispatch_reason,
            };
        });
        return true;
    }

    handleConnectionHealthNotification(payload) {
        if (payload.event_type !== CONNECTION_HEALTH_EVENT_TYPE) {
            return false;
        }
        this.connectionHealthBusRevision += 1;
        const applied = payload.connection_health
            ? this.applyConnectionHealth(payload.connection_health)
            : payload.item && this.applyConnectionHealthItem(payload.item);
        if (!applied) {
            this.connectionHealthInvalidationRevision += 1;
            this.scheduleConnectionHealthRefresh();
        }
        return true;
    }

    handleRetentionNotification(payload) {
        if (payload.retention_purged !== true) {
            return false;
        }
        this.discardListAnswersAfterPurge();
        const channelId = payload.channel_id;
        const conversation = this.loadedConversation(channelId);
        if (conversation) {
            // Even surviving messages can contain a reply quoting erased text.
            // Only the post-purge projection can safely render those snapshots.
            conversation.last_message = false;
        }
        if (channelId !== this.state.selectedChannelId) {
            this.scheduleSynchronization(false, false);
            return true;
        }
        this.timelineRequest += 1;
        this.resetTimelineContinuity();
        this.cancelSeenRetry();
        this.state.messages = [];
        this.state.replyTo = false;
        this.state.timelineHasMore = false;
        this.state.timelineHasMoreForward = false;
        this.state.nextBeforeMessageId = false;
        this.state.nextAfterChronologicalMessageId = false;
        this.state.timelineFirstUnreadMessageId = false;
        this.state.timelinePhase = "loading";
        this.reloadAfterRetentionPurge(channelId);
        return true;
    }

    async reloadAfterRetentionPurge(channelId) {
        const request = ++this.retentionTimelineRequest;
        const current = () =>
            !this.destroyed &&
            this.state.selectedChannelId === channelId &&
            request === this.retentionTimelineRequest;
        try {
            const refreshed = await this.refreshLoadedConversations({silent: true});
            if (!current()) {
                return false;
            }
            if (!refreshed) {
                this.state.timelinePhase = "error";
                return false;
            }
            // Honor the authoritative personal unread anchor. Purging is not a
            // command to jump to the tail or mark new messages as read.
            return await this.loadTimeline({reset: true, anchorUnread: true});
        } catch (_error) {
            if (current()) {
                this.state.timelinePhase = "error";
            }
            return false;
        }
    }

    /**
     * Apply a preference changed in another tab or by this tab's own action.
     *
     * The event is remembered with its revision whatever the filters; under
     * "Sem silenciadas" it also drops the row a newer mute hides. Pages and
     * tails answered before the event are resolved against the remembered
     * revision when they are applied, so none of them brings the row back.
     *
     * @param {Object} payload the conversation_preference_updated event
     * @returns {Boolean} whether the personal filter was applied
     */
    hideMutedElsewhere(payload) {
        const channelId = payload.channel_id;
        if (
            payload.event_type !== "conversation_preference_updated" ||
            !isPlainRecord(payload.preference) ||
            typeof payload.preference.muted !== "boolean"
        ) {
            return false;
        }
        if (!this.state.filters.excludeMuted) {
            this.rememberPreference(channelId, payload.preference);
            return false;
        }
        this.acceptPreferenceSnapshot({
            channel_id: channelId,
            preference: payload.preference,
        });
        this.state.conversations = this.state.conversations.filter(
            (row) => !this.hiddenByPersonalFilters(row)
        );
        return true;
    }

    /**
     * Discard list answers in flight after a local change.
     *
     * A visible first page is kept: it was requested after the change began
     * and its rows pass the same filters; the synchronization that follows the
     * change corrects the rest (L07-RSM-01).
     *
     * @returns {Boolean} whether answers were discarded
     */
    discardListAnswers() {
        if (this.state.listPhase === "loading") {
            return false;
        }
        this.listRequest += 1;
        return true;
    }

    /**
     * Discard every list answer computed before a retention purge.
     *
     * They may carry erased previews. A visible first page is replaced by a
     * refresh that takes over its visibility and errors and keeps the open
     * conversation (L07-AMEND2-02, LOAD-02).
     */
    discardListAnswersAfterPurge() {
        if (this.state.listPhase === "loading") {
            this.refreshLoadedConversations({silent: true});
            return;
        }
        this.listRequest += 1;
    }

    /**
     * Discard list answers after an action and synchronize the list again.
     *
     * A page being appended leaves its loading state; a visible first page
     * and an error stay as they are (L07-R14-B, RSM-03).
     */
    cancelListAnswers() {
        if (this.discardListAnswers() && this.state.listPhase === "loading_more") {
            this.state.listPhase = "ready";
        }
        this.scheduleSynchronization(false);
    }

    /**
     * Remember the newest preference of a conversation.
     *
     * Ordered only by the server revision; a tie keeps the one received last.
     * Only revisions above 0 are kept: revision 0 (no preference row) never
     * outranks a snapshot, so the memory holds at most the conversations this
     * user pinned or muted and never needs to forget one (L07-AMEND2-01).
     *
     * @param {Number} channelId the conversation
     * @param {Object} preference its preference as received
     */
    rememberPreference(channelId, preference) {
        const revision = conversationPreference({preference}).revision;
        if (
            !Number.isSafeInteger(channelId) ||
            !isPlainRecord(preference) ||
            !revision
        ) {
            return;
        }
        const known = this.preferenceMemory.get(channelId);
        if (known && conversationPreference({preference: known}).revision > revision) {
            return;
        }
        this.preferenceMemory.set(channelId, {...preference});
    }

    /**
     * Take the preferences of a received list page, used or not.
     *
     * A page superseded or followed by a failed page still proves those
     * revisions (L07-AMEND2-03): each one goes through the same transition as
     * any snapshot, so a loaded row takes the newer preference and a change of
     * a known mute synchronizes the list (AMEND2-05). Only the rows and their
     * content are discarded with the page.
     *
     * @param {Array} items the page items as received
     */
    rememberPagePreferences(items) {
        for (const item of Array.isArray(items) ? items : []) {
            if (isPlainRecord(item) && isPlainRecord(item.preference)) {
                this.acceptPreferenceSnapshot(
                    {channel_id: item.channel_id, preference: item.preference},
                    {fromPage: true}
                );
            }
        }
    }

    /**
     * Synchronize the list when a snapshot changes the known mute.
     *
     * Under "Sem silenciadas" a conversation that became eligible may belong
     * anywhere; the ordinary synchronization places it. Pins and unchanged
     * echoes synchronize nothing (L07-AMEND2-04).
     *
     * @param {Object|null} known the preference known before the snapshot
     * @param {Object} resolved the resolved snapshot
     * @param {Boolean} [requireKnown] a row never seen before is no change
     *   (the rows of a list page)
     * @returns {Boolean} whether the known mute changed
     */
    synchronizeOnMuteChange(known, resolved, requireKnown = false) {
        const changed = known
            ? conversationPreference({preference: known}).muted !==
              conversationPreference(resolved).muted
            : !requireKnown;
        if (changed && this.state.filters.excludeMuted) {
            this.scheduleSynchronization(false);
        }
        return changed;
    }

    /**
     * The newest known preference of a conversation, loaded or remembered.
     *
     * @param {Number} channelId the conversation
     * @returns {Object|null}
     */
    knownPreference(channelId) {
        const loaded = this.loadedConversation(channelId);
        const candidates = [
            loaded && loaded.preference,
            this.preferenceMemory.get(channelId),
        ].filter(isPlainRecord);
        let best = null;
        for (const candidate of candidates) {
            if (
                !best ||
                conversationPreference({preference: candidate}).revision >
                    conversationPreference({preference: best}).revision
            ) {
                best = candidate;
            }
        }
        return best;
    }

    /**
     * Take the personal preference of a validated server snapshot into account.
     *
     * Detail, action, read and bus answers pass here before their callers
     * decide anything else: the newest revision updates the loaded row and is
     * remembered, the row leaves when a personal filter now hides it, and a
     * change of its known mute synchronizes the list (emenda 2). Only the
     * preference is applied: a late answer never reinserts or selects a row.
     *
     * @param {Object} item a conversation snapshot from the server
     * @param {Object} [options] fromPage: the snapshot is a list page row
     * @returns {Boolean} whether its known mute changed
     */
    acceptPreferenceSnapshot(item, {fromPage = false} = {}) {
        if (!isPlainRecord(item) || !Number.isSafeInteger(item.channel_id)) {
            return false;
        }
        const known = this.knownPreference(item.channel_id);
        const resolved = this.resolvedPreference(item);
        const loaded = this.loadedConversation(item.channel_id);
        if (loaded && resolved === item && isPlainRecord(item.preference)) {
            loaded.preference = {...item.preference};
        }
        this.dropHiddenRow(item.channel_id);
        return this.synchronizeOnMuteChange(known, resolved, fromPage);
    }

    /**
     * Apply only the preference of an answer its flow no longer uses.
     *
     * @param {Object} payload the server answer
     * @param {Number} [channelId] the conversation the request was about;
     *   the answer's own channel when the request did not name one
     * @returns {Boolean} whether the list restarts
     */
    acceptLateConversationAnswer(payload, channelId = undefined) {
        try {
            validateEnvelope(payload);
        } catch (_error) {
            return false;
        }
        const expected = channelId === undefined ? payload.channel_id : channelId;
        if (!payload.item || payload.item.channel_id !== expected) {
            return false;
        }
        return this.acceptPreferenceSnapshot(payload.item);
    }

    /**
     * Keep the newest personal preference of a snapshot.
     *
     * Snapshots reach the client out of order (list pages, get_conversation,
     * action and read answers, bus events); the server revision orders them
     * against the loaded row and the remembered preference, so an older one
     * never undoes a newer mute or unmute, loaded or not, and any newer one
     * fixes a missed event. The winner is remembered.
     *
     * @param {Object} item a conversation snapshot
     * @returns {Object} the snapshot with the newest preference
     */
    resolvedPreference(item) {
        if (!isPlainRecord(item) || !Number.isSafeInteger(item.channel_id)) {
            return item;
        }
        const known = this.knownPreference(item.channel_id);
        if (
            !known ||
            conversationPreference({preference: known}).revision <=
                conversationPreference(item).revision
        ) {
            this.rememberPreference(item.channel_id, item.preference);
            return item;
        }
        return {...item, preference: {...known}};
    }

    /**
     * Whether a row is kept by the client only against the personal filters.
     *
     * A muted conversation stays listed under "Sem silenciadas" only while it
     * is open; the cached tail and page merges never keep it otherwise.
     *
     * @param {Object} item a loaded conversation row
     * @returns {Boolean}
     */
    hiddenByPersonalFilters(item) {
        return Boolean(
            this.state.filters.excludeMuted &&
                item.channel_id !== this.state.selectedChannelId &&
                item.preference &&
                item.preference.muted
        );
    }

    /**
     * Drop the row a silent refresh kept only because it was open.
     *
     * The server no longer listed it; once closed it follows the list.
     *
     * @param {Number} channelId the conversation that stopped being open
     * @returns {Boolean}
     */
    dropPreservedRow(channelId) {
        if (!channelId || this.preservedConversationChannelId !== channelId) {
            return false;
        }
        this.preservedConversationChannelId = false;
        this.state.conversations = this.state.conversations.filter(
            (row) => row.channel_id !== channelId
        );
        return true;
    }

    dropHiddenRow(channelId) {
        const item = channelId && this.loadedConversation(channelId);
        if (!item || !this.hiddenByPersonalFilters(item)) {
            return false;
        }
        this.state.conversations = this.state.conversations.filter(
            (row) => row.channel_id !== channelId
        );
        return true;
    }

    synchronizeNotification(payload) {
        this.noteSnapshotInvalidation(payload);
        this.noteUncertainBulkRead(payload);
        this.handleDeliveryNotification(payload);
        if (this.handleRetentionNotification(payload)) {
            return;
        }
        if (!SYNCHRONIZING_EVENTS.has(payload.event_type)) {
            return;
        }
        if (conversationUpdateScope(payload) === "identity_avatar") {
            this.scheduleAvatarSynchronization(payload.channel_id);
            return;
        }
        this.hideMutedElsewhere(payload);
        const refreshTimeline =
            payload.channel_id === this.state.selectedChannelId &&
            payload.event_type !== "conversation_preference_updated" &&
            (payload.event_type !== "delivery_updated" || payload.refresh === true);
        this.scheduleSynchronization(false, refreshTimeline);
    }

    noteSnapshotInvalidation(payload) {
        const channelId = payload.channel_id;
        let generations = null;
        if (SNAPSHOT_INVALIDATING_EVENTS.has(payload.event_type)) {
            generations = this.conversationGenerations;
        } else if (
            payload.event_type === "member_seen" &&
            Boolean(this.currentUserId) &&
            payload.user_id === this.currentUserId
        ) {
            generations = this.ownReadGenerations;
        }
        if (generations && Number.isSafeInteger(channelId)) {
            generations.set(channelId, (generations.get(channelId) || 0) + 1);
        }
    }

    /**
     * A read by this user while a bulk action has an unknown outcome.
     *
     * It may be that action committing late: the list is synchronized sooner.
     * It never settles the outcome (an ordinary read sends the same event).
     *
     * @param {Object} payload the notification
     */
    noteUncertainBulkRead(payload) {
        if (
            this.bulkReadUncertain &&
            payload.event_type === "member_seen" &&
            Boolean(this.currentUserId) &&
            payload.user_id === this.currentUserId
        ) {
            this.scheduleSynchronization(false);
        }
    }

    conversationGeneration(channelId) {
        return this.conversationGenerations.get(channelId) || 0;
    }

    onNotification(event) {
        const detail = event && event.detail;
        if (Array.isArray(detail) && detail.length) {
            // Any notification could only have arrived through an open bus.  This
            // also covers a tab joining an already-connected SharedWorker, for
            // which Odoo 16 does not replay a `connect` event to the new client.
            this.onConnect();
        }
        const notifications = contactCenterNotifications(detail);
        if (!notifications.length) {
            return;
        }
        for (const payload of notifications) {
            if (payload.event_type === "conversation_deleted") {
                this.forgetDeletedConversation(payload.channel_id);
                this.scheduleSynchronization(false, false);
                continue;
            }
            if (this.deletedConversationIds.has(payload.channel_id)) {
                continue;
            }
            this.handleAttentionNotification(payload);
            if (this.handleConnectionHealthNotification(payload)) {
                continue;
            }
            if (this.handleAttributionNotification(payload)) {
                continue;
            }
            if (this.handleProductivityNotification(payload)) {
                continue;
            }
            this.synchronizeNotification(payload);
        }
    }

    scheduleSynchronization(reconnect, refreshTimeline = false, {urgent = false} = {}) {
        if (this.destroyed) {
            return false;
        }
        this.syncFullRequested = true;
        this.syncReconnect = this.syncReconnect || reconnect;
        this.syncTimeline = this.syncTimeline || refreshTimeline;
        return this.syncRefresh.schedule({urgent});
    }

    scheduleAvatarSynchronization(channelId) {
        if (this.destroyed) {
            return false;
        }
        const quiescent =
            !this.listLoadsInFlight &&
            !this.conversationDetailReadsInFlight &&
            !this.syncFullActive &&
            !this.syncFullRequested &&
            this.state.listPhase === "ready" &&
            this.listWindowFilterRevision === this.filterRevision;
        if (quiescent && !this.loadedConversation(channelId)) {
            return false;
        }
        this.syncAvatarChannels.add(channelId);
        if (this.syncAvatarChannels.size > 100) {
            this.syncAvatarChannels.clear();
            return this.scheduleSynchronization(false);
        }
        return this.syncRefresh.schedule();
    }

    async refreshConversationAvatars(channelIds) {
        // An older list/detail answer must never overwrite an accepted patch.
        if (
            this.listLoadsInFlight ||
            this.conversationDetailReadsInFlight ||
            this.state.listPhase !== "ready" ||
            this.listWindowFilterRevision !== this.filterRevision
        ) {
            this.scheduleSynchronization(false);
            return false;
        }
        const rows = new Map(
            channelIds
                .map((id) => [id, this.loadedConversation(id)])
                .filter(([, row]) => row)
        );
        if (!rows.size) {
            return true;
        }
        const request = this.listRequest;
        const revision = this.filterRevision;
        const generations = new Map(
            [...rows.keys()].map((id) => [id, this.conversationGeneration(id)])
        );
        try {
            const payload = await this.call(
                "get_conversation_avatars",
                [[...rows.keys()]],
                {},
                {silent: true, automatic: true}
            );
            validateEnvelope(payload);
            if (this.destroyed) {
                return false;
            }
            const items = new Map();
            if (!Array.isArray(payload.items) || payload.items.length !== rows.size) {
                throw new TypeError("A projeção de fotos está incompleta.");
            }
            for (const item of payload.items) {
                const row = item && rows.get(item.channel_id);
                if (
                    !row ||
                    items.has(item.channel_id) ||
                    !row.identity ||
                    !Number.isSafeInteger(item.identity_id) ||
                    item.identity_id <= 0 ||
                    item.identity_id !== row.identity.id ||
                    (item.avatar_url !== false &&
                        (typeof item.avatar_url !== "string" ||
                            !new RegExp(
                                `^/contact_center/conversation/${item.channel_id}/avatar\\?v=[a-f0-9]{0,12}$`
                            ).test(item.avatar_url)))
                ) {
                    throw new TypeError("A projeção de fotos é inválida.");
                }
                items.set(item.channel_id, item);
            }
            if (
                this.syncFullRequested ||
                this.listLoadsInFlight ||
                this.conversationDetailReadsInFlight ||
                request !== this.listRequest ||
                revision !== this.filterRevision ||
                [...rows].some(
                    ([id, row]) =>
                        row !== this.loadedConversation(id) ||
                        generations.get(id) !== this.conversationGeneration(id)
                )
            ) {
                this.scheduleSynchronization(false);
                return false;
            }
            this.state.conversations = this.state.conversations.map((row) => {
                const item = items.get(row.channel_id);
                return item
                    ? {
                          ...row,
                          identity: {...row.identity, avatar_url: item.avatar_url},
                      }
                    : row;
            });
            return true;
        } catch (_error) {
            if (!this.destroyed) {
                this.syncCycleFailed = true;
                this.scheduleSynchronization(false);
            }
            return false;
        }
    }

    async runSynchronization() {
        const avatarOnly =
            this.syncAvatarChannels.size &&
            !this.syncFullRequested &&
            !this.syncReconnect &&
            !this.syncTimeline;
        const ids = [...this.syncAvatarChannels];
        this.syncAvatarChannels.clear();
        this.syncCycleStopped = false;
        this.syncCycleFailed = false;
        if (avatarOnly) {
            this.syncAvatarActive = true;
            try {
                return await this.refreshConversationAvatars(ids);
            } finally {
                this.syncAvatarActive = false;
            }
        }
        this.syncFullRequested = false;
        this.syncFullActive = true;
        this.lastFullSynchronizationStarted = this.syncRefresh.now();
        try {
            return await this.runFullSynchronization();
        } finally {
            this.syncFullActive = false;
            this.syncFullRequested =
                this.syncFullRequested || this.syncCycleStopped || this.syncCycleFailed;
        }
    }

    async runFullSynchronization() {
        const reconnect = this.syncReconnect;
        const timeline = this.syncTimeline;
        this.syncReconnect = false;
        this.syncTimeline = false;
        this.syncCycleStopped = false;
        this.syncCycleFailed = false;
        this.syncActiveWaiters.push(...this.syncWaiters);
        this.syncWaiters = [];
        this.syncListPending = true;
        try {
            await this.refreshLoadedConversations({
                silent: true,
                automatic: true,
            });
        } finally {
            this.syncListPending = false;
            this.releaseSynchronizationWaiters();
        }
        const deferred = () =>
            this.destroyed || this.syncCycleStopped || this.visibilityDocument.hidden;
        if (!deferred() && this.state.selectedChannelId) {
            await this.refreshSelectedConversation({
                silent: true,
                automatic: true,
            });
            if (
                !deferred() &&
                this.state.selectedChannelId &&
                (timeline || reconnect)
            ) {
                await this.refreshLatestTimeline({automatic: true});
            }
        }
        if (deferred() && !this.destroyed) {
            this.syncReconnect = this.syncReconnect || reconnect;
            this.syncTimeline = this.syncTimeline || timeline;
            this.syncFullRequested = true;
            this.syncRefresh.schedule();
            return false;
        }
        // Skipped history and superseded snapshots are neutral; caught read or
        // validation failures keep the backoff, including nested revalidation.
        return !this.syncCycleFailed;
    }
}
