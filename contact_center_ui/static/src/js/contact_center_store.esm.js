/** @odoo-module **/

import {
    CONNECTION_HEALTH_EVENT_TYPE,
    contactCenterNotifications,
    conversationPreference,
    conversationStateMeta,
    conversationUiPolicy,
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
    validateEnvelope,
} from "./contact_center_model.esm";
import {_t} from "@web/core/l10n/translation";
import {browser} from "@web/core/browser/browser";
import {session} from "@web/session";

const API_MODEL = "contact.center.ui.api";
const LIST_LIMIT = 50;
const REALTIME_REFRESH_LIMIT = 200;
const TIMELINE_LIMIT = 50;
const TIMELINE_REFRESH_LIMIT = 100;
const TIMELINE_FORWARD_MAX_PAGES = 20;
const TIMELINE_MODES = new Set(["reset", "older", "newer", "refresh_latest"]);
const SEEN_RETRY_DELAYS = Object.freeze([1000, 3000, 10000]);
const CONNECTION_HEALTH_INVALIDATION_DELAY = 160;
const INBOX_DENSITY_STORAGE_KEY = "contact_center_ui.inbox_density.v1";
const INBOX_DENSITIES = new Set(["comfortable", "compact"]);
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
        query.after_message_id = afterMessageId;
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
        cases: [],
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
        cases: productivityItems(payload, "cases"),
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
            payload.next_after_message_id !== items[items.length - 1].message_id) ||
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

function replyMatches(replyTo, replyId) {
    return (!replyTo && !replyId) || (replyTo && replyTo.message_id === replyId);
}

function companyMutationIdentity(payload, partnerId, expectedCompanyId = false) {
    if (!isPlainRecord(payload) || !isPlainRecord(payload.identity)) {
        throw new Error("O servidor não confirmou o vínculo da empresa.");
    }
    const identity = payload.identity;
    const partner = identity.partner;
    if (!isPlainRecord(partner)) {
        throw new Error("O servidor não confirmou o vínculo da empresa.");
    }
    const company = normalizePartnerCompany(payload.company);
    const linkedCompany = partnerCompanyForIdentity(identity);
    if (!company || !linkedCompany) {
        throw new Error("O servidor não confirmou o vínculo da empresa.");
    }
    if (partner.id !== partnerId || partner.is_company !== false) {
        throw new Error("O servidor não confirmou o vínculo da empresa.");
    }
    if (partner.company_linking_allowed !== false) {
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
        inboxDensityStorage = browserLocalStorage(),
        operationStorage = browserSessionStorage(),
        operationDatabase = false,
        operationNow = () => Date.now(),
        operationCrypto = window.crypto,
        attention = false,
    }) {
        this.orm = orm;
        this.busService = busService;
        this.notification = notification;
        this.inboxDensityStorage = inboxDensityStorage;
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
            nextAfterMessageId: false,
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
            inboxDensity: loadInboxDensityPreference(inboxDensityStorage),
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
                state: "open",
                accountId: false,
                query: "",
                responsibility: "all",
                unreadOnly: false,
                conversationType: false,
                tagId: false,
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
                phase: "idle",
                query: "",
                results: [],
                error: "",
            },
            companyOperationsRevision: 0,
        });
        this.listRequest = 0;
        this.preservedConversationChannelId = false;
        this.timelineRequest = 0;
        this.timelineForwardChannelId = false;
        this.timelineForwardCursor = false;
        this.timelineForwardPageCount = 0;
        this.timelineContiguousChannelId = false;
        this.timelineContiguousCursor = false;
        this.seenRetryTimer = null;
        this.seenRetryToken = 0;
        this.attributionRequest = 0;
        this.searchTimer = null;
        this.contactSearchTimer = null;
        this.contactLinkerRequest = 0;
        this.companySearchTimer = null;
        this.companyLinkerRequest = 0;
        this.pendingCompanyOperations = new Set();
        this.conversationSelectionGuard = null;
        this.syncTimer = null;
        this.consistencySyncTimer = null;
        this.healthSyncTimer = null;
        this.healthSyncAttempt = 0;
        this.connectionHealthBusRevision = 0;
        this.syncReconnect = false;
        this.syncTimeline = false;
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
        if (this.attention) {
            this.attention.setStateListener((snapshot) => {
                if (!this.destroyed) {
                    this.state.attention = snapshot;
                }
            });
        }
    }

    get selectedConversation() {
        return (
            this.state.conversations.find(
                (item) =>
                    isRenderableConversation(item) &&
                    item.channel_id === this.state.selectedChannelId
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

    get responsibilityVisibleConversations() {
        return filterConversationsByResponsibility(
            this.state.conversations,
            this.state.filters.responsibility,
            this.currentUserId
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

    async call(method, args = [], kwargs = {}) {
        return this.orm.call(API_MODEL, method, args, kwargs);
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
        this.destroyed = true;
        this.cancelSeenRetry();
        this.started = false;
        this.busService.removeEventListener("notification", this.onNotification);
        this.busService.removeEventListener("connect", this.onConnect);
        this.busService.removeEventListener("reconnect", this.onReconnect);
        this.busService.removeEventListener("reconnecting", this.onReconnecting);
        this.busService.removeEventListener("disconnect", this.onDisconnect);
        for (const timer of [this.searchTimer, this.syncTimer]) {
            if (timer !== null) {
                browser.clearTimeout(timer);
            }
        }
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
            this.state.phase = "ready";
            await this.loadConversations({reset: true, selectFirst: true});
        } catch (error) {
            if (error instanceof RangeError) {
                this.state.phase = "unsupported";
                return;
            }
            this.state.phase = "error";
            this.notify(errorMessage(error), {type: "danger", title: "Contact Center"});
        }
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

    async refreshConnectionHealth() {
        if (this.destroyed) {
            return false;
        }
        const busRevision = this.connectionHealthBusRevision;
        try {
            const payload = await this.call("bootstrap");
            validateEnvelope(payload);
            if (this.connectionHealthBusRevision !== busRevision) {
                return true;
            }
            return this.applyConnectionHealth(payload.connection_health);
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
        if (this.state.filters.state) {
            filters.state = this.state.filters.state;
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
        if (this.state.filters.unreadOnly) {
            filters.unread_only = true;
        }
        if (["direct", "group"].includes(this.state.filters.conversationType)) {
            filters.conversation_type = this.state.filters.conversationType;
        }
        if (
            Number.isSafeInteger(this.state.filters.tagId) &&
            this.state.filters.tagId > 0
        ) {
            filters.tag_id = this.state.filters.tagId;
        }
        if (
            ["overdue", "today", "planned"].includes(this.state.filters.activityTiming)
        ) {
            filters.activity_timing = this.state.filters.activityTiming;
        }
        return filters;
    }

    clearConversationSelection({closePanes = false} = {}) {
        this.cancelSeenRetry();
        this.timelineRequest += 1;
        this.resetTimelineContinuity();
        this.resetAttribution();
        this.resetProductivity();
        this.resetQuickReplies();
        this.state.selectedChannelId = false;
        this.state.timelineChannelId = false;
        this.state.messages = [];
        this.state.timelineHasMore = false;
        this.state.nextBeforeMessageId = false;
        this.state.timelineHasMoreForward = false;
        this.state.nextAfterMessageId = false;
        this.state.timelineFirstUnreadMessageId = false;
        this.state.timelinePhase = "idle";
        this.state.replyTo = false;
        this.closeContactLinker();
        this.closeCompanyLinker();
        if (closePanes) {
            this.state.detailsOpen = false;
            this.state.mobilePane = "list";
        }
    }

    isCurrentConversationRequest(request) {
        return request === this.listRequest && !this.destroyed;
    }

    applyConversationPage(payload, {reset, silent, previousConversation}) {
        const items = normalizedConversationItems(payload.items);
        if (reset) {
            this.state.conversations = items;
            this.preservedConversationChannelId = false;
            const previousIsMissing =
                previousConversation &&
                !items.some(
                    (item) => item.channel_id === previousConversation.channel_id
                );
            if (
                silent &&
                previousIsMissing &&
                this.state.filters.responsibility === "all"
            ) {
                this.state.conversations.push(previousConversation);
                this.preservedConversationChannelId = previousConversation.channel_id;
            }
        } else {
            const byId = new Map(
                this.state.conversations
                    .filter(isRenderableConversation)
                    .map((item) => [item.channel_id, item])
            );
            for (const item of items) {
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
        if (payload.total !== false && payload.total !== undefined) {
            this.state.conversationTotal = payload.total;
        }
        this.state.conversationsHaveMore = Boolean(payload.has_more);
        this.state.nextConversationCursor = payload.next_cursor || false;
        this.state.listPhase = "ready";
    }

    async reconcileConversationSelection({
        reset,
        silent,
        selectFirst,
        previousSelected,
    }) {
        const visibleConversations = this.responsibilityVisibleConversations;
        const selectedStillVisible = visibleConversations.some(
            (item) => item.channel_id === previousSelected
        );
        const selectedConversationWasRemoved =
            Boolean(previousSelected) && !selectedStillVisible;
        const responsibilitySelectionChanged =
            this.state.filters.responsibility !== "all" &&
            selectedConversationWasRemoved;
        if (
            !selectedStillVisible &&
            reset &&
            (!silent || responsibilitySelectionChanged)
        ) {
            this.clearConversationSelection({
                closePanes: !visibleConversations.length,
            });
        }
        if (
            (selectFirst || responsibilitySelectionChanged) &&
            !this.state.selectedChannelId &&
            visibleConversations.length
        ) {
            await this.selectConversation(visibleConversations[0].channel_id, {
                preservePane: true,
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
                type: "danger",
                title: "Conversas indisponíveis",
            });
        }
        return false;
    }

    async loadConversations({reset = false, selectFirst = false, silent = false} = {}) {
        if (this.destroyed) {
            return false;
        }
        const request = ++this.listRequest;
        const previousConversation = this.selectedConversation;
        if (!silent) {
            this.state.listPhase = reset ? "loading" : "loading_more";
        }
        const cursor = reset ? false : this.state.nextConversationCursor;
        try {
            const payload = await this.call("list_conversations", [], {
                limit: LIST_LIMIT,
                filters: this.conversationFilters(),
                cursor,
            });
            validateEnvelope(payload);
            if (!this.isCurrentConversationRequest(request)) {
                return false;
            }
            const previousSelected = this.state.selectedChannelId;
            this.applyConversationPage(payload, {
                reset,
                silent,
                previousConversation,
            });
            await this.reconcileConversationSelection({
                reset,
                silent,
                selectFirst,
                previousSelected,
            });
            return true;
        } catch (error) {
            return this.conversationLoadFailed(error, request, silent);
        }
    }

    async refreshLoadedConversations({silent = true} = {}) {
        if (this.destroyed) {
            return false;
        }
        const request = ++this.listRequest;
        const previousConversation = this.selectedConversation;
        const previousSelected = this.state.selectedChannelId;
        const cachedWindow = this.state.conversations.filter(
            (item) =>
                isRenderableConversation(item) &&
                item.channel_id !== this.preservedConversationChannelId
        );
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
                const payload = await this.call("list_conversations", [], {
                    limit: Math.min(100, targetCount - items.length),
                    filters: this.conversationFilters(),
                    cursor,
                });
                validateEnvelope(payload);
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
                cachedWindow,
                cachedHasMore,
                cachedNextCursor,
                targetCount,
            });
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
            await this.reconcileConversationSelection({
                reset: true,
                silent,
                selectFirst: false,
                previousSelected,
            });
            return true;
        } catch (error) {
            return this.conversationLoadFailed(error, request, silent);
        }
    }

    loadMoreConversations() {
        if (
            this.state.listPhase === "loading_more" ||
            !this.state.conversationsHaveMore
        ) {
            return Promise.resolve(false);
        }
        return this.loadConversations({reset: false});
    }

    setFilter(name, value) {
        if (!(name in this.state.filters)) {
            return false;
        }
        if (name === "responsibility") {
            if (!isResponsibilityScope(value)) {
                return false;
            }
            this.state.filters.responsibility = value;
            return this.loadConversations({reset: true, selectFirst: true});
        }
        let normalizedValue = value;
        if (name === "unreadOnly") {
            normalizedValue = value === true;
        } else if (name === "conversationType") {
            normalizedValue = ["direct", "group"].includes(value) ? value : false;
        } else if (name === "tagId") {
            const tagId = Number(value);
            normalizedValue = Number.isSafeInteger(tagId) && tagId > 0 ? tagId : false;
        } else if (name === "activityTiming") {
            normalizedValue = ["overdue", "today", "planned"].includes(value)
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
                this.loadConversations({reset: true, selectFirst: true});
            }, 260);
            return;
        }
        this.loadConversations({reset: true, selectFirst: true});
    }

    toggleInboxDensity() {
        const density =
            this.state.inboxDensity === "compact" ? "comfortable" : "compact";
        this.state.inboxDensity = density;
        saveInboxDensityPreference(density, this.inboxDensityStorage);
        return density;
    }

    async selectConversation(channelId, {preservePane = false} = {}) {
        if (
            this.state.selectedChannelId === channelId &&
            this.state.timelineChannelId === channelId &&
            this.state.messages.length
        ) {
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
            return false;
        }
        this.state.selectedChannelId = channelId;
        if (changedConversation) {
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
            this.state.nextAfterMessageId = false;
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
        return this.loadTimeline({reset: true});
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
            requestValues.case_id || false,
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
        latestIncoming,
        reanchorLatest,
        refreshHasOverlap,
        hasMore,
    }) {
        if (mode === "reset" || reanchorLatest) {
            this.resetTimelineContinuity();
            if (latestIncoming) {
                this.advanceTimelineContinuity(channelId, latestIncoming.message_id);
            }
            return;
        }
        if (!latestIncoming) {
            return;
        }
        if (mode === "older") {
            if (this.timelineContiguousChannelId !== channelId) {
                this.advanceTimelineContinuity(channelId, latestIncoming.message_id);
            }
            return;
        }
        if (refreshHasOverlap || !hasMore) {
            this.advanceTimelineContinuity(channelId, latestIncoming.message_id);
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
        this.state.messages = mergeTimelineItems(existing, incomingPage, {
            prepend: false,
        });
        this.state.timelineChannelId = channelId;
        this.advanceTimelineContinuity(channelId, payload.next_after_message_id);
        this.state.timelineHasMoreForward = Boolean(payload.has_more_forward);
        this.state.nextAfterMessageId = payload.next_after_message_id || false;
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
        this.state.nextAfterMessageId = payload.next_after_message_id || false;
        this.state.timelineFirstUnreadMessageId =
            Number.isSafeInteger(payload.anchor_message_id) &&
            payload.anchor_message_id > 0
                ? payload.anchor_message_id
                : false;
    }

    applyTimelinePage(payload, mode, channelId, {markSeen = true} = {}) {
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
        const latestIncoming = incomingPage[incomingPage.length - 1];
        this.updateTimelineContinuityAfterPage({
            mode,
            channelId,
            latestIncoming,
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
        const latest = this.state.messages[this.state.messages.length - 1];
        if (
            latest &&
            mode !== "refresh_latest" &&
            markSeen &&
            !this.state.timelineFirstUnreadMessageId
        ) {
            this.markSeen(latest.message_id);
        }
    }

    timelineNeedsForwardRecovery(payload, channelId) {
        if (this.timelineForwardChannelId === channelId && this.timelineForwardCursor) {
            return true;
        }
        return this.timelineHeadHasContiguousGap(payload, channelId);
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
        this.state.messages = mergeTimelineItems(this.state.messages, items, {
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

    async fetchForwardTimelinePage(request, channelId, afterMessageId, limit) {
        const payload = await this.call("get_timeline", [channelId], {
            after_message_id: afterMessageId,
            limit,
        });
        validateEnvelope(payload);
        if (!this.isCurrentTimelineRequest(request, channelId)) {
            return false;
        }
        if (payload.channel_id !== channelId) {
            throw new TypeError("A conversa da paginação incremental é inválida.");
        }
        return forwardTimelinePage(payload, afterMessageId);
    }

    async fetchCurrentTimelineHead(request, channelId, limit, invalidMessage) {
        const payload = await this.call("get_timeline", [channelId], {
            before_message_id: false,
            limit,
        });
        validateEnvelope(payload);
        if (!this.isCurrentTimelineRequest(request, channelId)) {
            return false;
        }
        if (payload.channel_id !== channelId) {
            throw new TypeError(invalidMessage);
        }
        return payload;
    }

    async reanchorTimelineAfterForwardLimit(request, channelId, limit) {
        const payload = await this.fetchCurrentTimelineHead(
            request,
            channelId,
            limit,
            "A conversa da recentralização de mensagens é inválida."
        );
        if (!payload) {
            return false;
        }
        this.resetTimelineForwardRecovery();
        this.applyTimelinePage(payload, "reset", channelId, {markSeen: false});
        this.notify(
            "A conversa recebeu um volume alto durante a ausência. O histórico foi recentralizado e continua disponível ao carregar mensagens anteriores.",
            {type: "warning", title: "Histórico recentralizado"}
        );
        return true;
    }

    async finishTimelineForwardRecovery(request, channelId, limit) {
        const payload = await this.fetchCurrentTimelineHead(
            request,
            channelId,
            limit,
            "A conversa da atualização final de mensagens é inválida."
        );
        if (!payload) {
            return false;
        }
        if (this.timelineHeadHasContiguousGap(payload, channelId)) {
            // More than one head window arrived while the bounded catch-up was
            // running. Preserve the confirmed cursor for the next fenced cycle.
            this.scheduleSynchronization(false, true);
            return true;
        }
        this.resetTimelineForwardRecovery();
        this.applyTimelinePage(payload, "refresh_latest", channelId);
        return true;
    }

    async recoverTimelineGap(request, channelId, limit) {
        let afterMessageId = this.beginTimelineForwardRecovery(channelId);
        let hasMore = true;
        while (hasMore && this.timelineForwardPageCount < TIMELINE_FORWARD_MAX_PAGES) {
            const page = await this.fetchForwardTimelinePage(
                request,
                channelId,
                afterMessageId,
                limit
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
            return this.reanchorTimelineAfterForwardLimit(request, channelId, limit);
        }
        return this.finishTimelineForwardRecovery(request, channelId, limit);
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
        this.scheduleSynchronization(false, true);
        return true;
    }

    timelineLoadFailed(error, request, channelId, silent) {
        if (!this.isCurrentTimelineRequest(request, channelId)) {
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
                this.state.nextAfterMessageId,
                firstUnreadMessageId
            );
            const payload = await this.call("get_timeline", [channelId], query);
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
                return await this.recoverTimelineGap(request, channelId, limit);
            }
            this.applyTimelinePage(payload, mode, channelId);
            return true;
        } catch (error) {
            return this.timelineLoadFailed(error, request, channelId, silent);
        }
    }

    loadOlderMessages() {
        if (
            this.state.timelinePhase === "loading_more" ||
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
            this.state.timelinePhase === "loading_newer" ||
            !this.state.timelineHasMoreForward ||
            !this.state.nextAfterMessageId
        ) {
            return Promise.resolve(false);
        }
        return this.loadTimeline({reset: false, mode: "newer"});
    }

    jumpToLatest() {
        return this.loadTimeline({reset: true, anchorUnread: false});
    }

    refreshLatestTimeline() {
        if (this.state.timelineHasMoreForward) {
            return Promise.resolve(false);
        }
        return this.loadTimeline({
            mode: "refresh_latest",
            silent: true,
            limit: TIMELINE_REFRESH_LIMIT,
        });
    }

    cancelSeenRetry() {
        this.seenRetryToken += 1;
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
            this.scheduleSynchronization(false, false);
            return false;
        }
        this.seenRetryTimer = this.realtimeTimer.setTimeout(async () => {
            this.seenRetryTimer = null;
            await this.persistSeenPointer(channelId, messageId, token, attempt + 1);
        }, SEEN_RETRY_DELAYS[attempt]);
        return true;
    }

    async persistSeenPointer(channelId, messageId, token, attempt) {
        try {
            await this.call("mark_seen", [channelId, messageId]);
            if (
                this.destroyed ||
                token !== this.seenRetryToken ||
                channelId !== this.state.selectedChannelId
            ) {
                return false;
            }
            const latest = this.state.messages[this.state.messages.length - 1];
            if (
                !this.state.timelineHasMoreForward &&
                (!latest || latest.message_id <= messageId)
            ) {
                const conversation = this.selectedConversation;
                if (conversation) {
                    conversation.unread_count = 0;
                    conversation.first_unread_message_id = false;
                }
                this.state.timelineFirstUnreadMessageId = false;
            } else {
                this.scheduleSynchronization(false, false);
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
            this.destroyed
        ) {
            return Promise.resolve(false);
        }
        this.cancelSeenRetry();
        return this.persistSeenPointer(channelId, messageId, this.seenRetryToken, 0);
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

    prepareSendContext(conversation, cleanBody, cleanMediaRefs) {
        const policy = conversationUiPolicy(conversation);
        if (
            !canStartSend(
                conversation,
                cleanBody,
                cleanMediaRefs,
                this.isSending(conversation && conversation.channel_id)
            ) ||
            (cleanMediaRefs.length && !policy.allow_attachments)
        ) {
            return false;
        }
        if (!this.state.replyTo) {
            return {replyId: false};
        }
        const target = this.state.messages.find(
            (message) => message.message_id === this.state.replyTo.message_id
        );
        if (
            !policy.allow_reply ||
            !target ||
            !target.actions ||
            target.actions.reply !== true
        ) {
            this.state.replyTo = false;
            return false;
        }
        return {replyId: target.message_id};
    }

    async sendMessage(body, mediaRefs = []) {
        const conversation = this.selectedConversation;
        const cleanBody = typeof body === "string" ? body.trim() : "";
        const cleanMediaRefs = normalizedMediaRefs(mediaRefs);
        const sendContext = this.prepareSendContext(
            conversation,
            cleanBody,
            cleanMediaRefs
        );
        if (!sendContext) {
            return false;
        }
        const channelId = conversation.channel_id;
        const replyId = sendContext.replyId;
        const signature = `send-message:${channelId}:${
            replyId || ""
        }:${cleanBody}:${cleanMediaRefs.join(",")}`;
        const requestId = this.operationRequestId(signature);
        this.setChannelSending(channelId, true);
        try {
            const payload = await this.call("send_message", [
                channelId,
                cleanBody,
                replyId || false,
                requestId,
                cleanMediaRefs,
            ]);
            validateOperationEnvelope(payload, {
                channelId,
                requestId,
                requiredRecords: ["message"],
            });
            if (this.state.selectedChannelId === channelId) {
                this.state.messages = mergeTimelineItems(
                    this.state.messages,
                    [payload.message],
                    {prepend: false}
                );
            }
            this.completeOperationIntent(signature, requestId);
            if (
                this.state.selectedChannelId === channelId &&
                replyMatches(this.state.replyTo, replyId)
            ) {
                this.state.replyTo = false;
            }
            await this.refreshLoadedConversations({silent: true});
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Mensagem não enviada",
                sticky: true,
            });
            return false;
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

    replaceConversation(item) {
        const normalizedItem = normalizeConversationGroup(item);
        if (!isRenderableConversation(normalizedItem)) {
            return false;
        }
        const stateFilter = this.state.filters.state;
        if (stateFilter && normalizedItem.state !== stateFilter) {
            this.state.conversations = this.state.conversations.filter(
                (conversation) => conversation.channel_id !== normalizedItem.channel_id
            );
            if (normalizedItem.channel_id === this.state.selectedChannelId) {
                this.clearConversationSelection({closePanes: true});
            }
            return true;
        }
        const index = this.state.conversations.findIndex(
            (conversation) =>
                isRenderableConversation(conversation) &&
                conversation.channel_id === normalizedItem.channel_id
        );
        if (index >= 0) {
            this.state.conversations.splice(index, 1, normalizedItem);
        } else {
            this.state.conversations.unshift(normalizedItem);
        }
        if (
            normalizedItem.channel_id === this.state.selectedChannelId &&
            this.state.filters.responsibility !== "all" &&
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

    async updateConversation(patch) {
        const channelId = this.state.selectedChannelId;
        if (!channelId) {
            return false;
        }
        try {
            const payload = await this.call("update_conversation", [channelId, patch]);
            validateEnvelope(payload);
            if (payload.item) {
                this.replaceConversation(payload.item);
            }
            if (payload.removed_from_conversation || !this.state.selectedChannelId) {
                await this.loadConversations({reset: true, selectFirst: true});
            }
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Conversa não atualizada",
            });
            return false;
        }
    }

    async setConversationState(state) {
        return this.updateConversation({state});
    }

    async setConversationPreference(patch) {
        const channelId = this.state.selectedChannelId;
        if (
            !channelId ||
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
            if (!payload.item || !this.replaceConversation(payload.item)) {
                throw new TypeError(
                    "A preferência retornada pelo servidor é inválida."
                );
            }
            await this.loadConversations({reset: true, silent: true});
            return true;
        } catch (error) {
            this.notify(errorMessage(error), {
                type: "danger",
                title: "Preferência não atualizada",
            });
            return false;
        }
    }

    toggleConversationPinned() {
        const preference = conversationPreference(this.selectedConversation);
        return this.setConversationPreference({pinned: !preference.pinned});
    }

    toggleConversationMuted() {
        const preference = conversationPreference(this.selectedConversation);
        return this.setConversationPreference({muted: !preference.muted});
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
                await this.loadConversations({reset: true, selectFirst: true});
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
                partner.company_linking_allowed === true &&
                partner.company === false
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

    async refreshSelectedConversation({silent = false} = {}) {
        const channelId = this.state.selectedChannelId;
        if (!channelId) {
            return false;
        }
        try {
            const payload = await this.call("get_conversation", [channelId]);
            validateEnvelope(payload);
            if (channelId === this.state.selectedChannelId) {
                this.reconcileTagCatalog(payload.item && payload.item.tags);
                this.replaceConversation(payload.item);
                if (!this.state.selectedChannelId) {
                    await this.loadConversations({reset: true, selectFirst: true});
                }
            }
            return true;
        } catch (error) {
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
            ]);
            validateEnvelope(payload);
            if (this.destroyed) {
                return false;
            }
            const identity = companyMutationIdentity(
                payload,
                operation.partnerId,
                companyPartnerId
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
            ]);
            validateEnvelope(payload);
            if (this.destroyed) {
                return false;
            }
            const identity = companyMutationIdentity(payload, operation.partnerId);
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
        const conversation = this.state.conversations.find(
            (item) => item.channel_id === payload.channel_id
        );
        if (
            this.attention &&
            payload.personal_attention !== false &&
            !conversationPreference(conversation).muted
        ) {
            this.attention.receive(payload);
        }
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
        if (payload.connection_health) {
            this.applyConnectionHealth(payload.connection_health);
        } else if (!payload.item || !this.applyConnectionHealthItem(payload.item)) {
            this.scheduleConnectionHealthRefresh();
        }
        return true;
    }

    synchronizeNotification(payload) {
        this.handleDeliveryNotification(payload);
        if (!SYNCHRONIZING_EVENTS.has(payload.event_type)) {
            return;
        }
        const refreshTimeline =
            payload.channel_id === this.state.selectedChannelId &&
            payload.event_type !== "conversation_preference_updated" &&
            (payload.event_type !== "delivery_updated" || payload.refresh === true);
        this.scheduleSynchronization(false, refreshTimeline);
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

    scheduleSynchronization(reconnect, refreshTimeline = false) {
        this.syncReconnect = this.syncReconnect || reconnect;
        this.syncTimeline = this.syncTimeline || refreshTimeline;
        if (this.syncTimer !== null) {
            return;
        }
        this.syncTimer = browser.setTimeout(async () => {
            this.syncTimer = null;
            const synchronizedReconnect = this.syncReconnect;
            const synchronizeTimeline = this.syncTimeline;
            this.syncReconnect = false;
            this.syncTimeline = false;
            await this.refreshLoadedConversations({silent: true});
            if (this.state.selectedChannelId) {
                await this.refreshSelectedConversation({silent: true});
                if (
                    this.state.selectedChannelId &&
                    (synchronizeTimeline || synchronizedReconnect)
                ) {
                    await this.refreshLatestTimeline();
                }
            }
            if (synchronizedReconnect) {
                this.state.realtime = "online";
            }
        }, 120);
    }
}
