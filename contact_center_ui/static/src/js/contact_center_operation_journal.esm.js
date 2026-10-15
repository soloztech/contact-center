/** @odoo-module **/

const OPERATION_JOURNAL_VERSION = 1;
const OPERATION_JOURNAL_PREFIX = "contact_center_ui.operation_journal";
const OPERATION_JOURNAL_MAX_ENTRIES = 200;
export const OPERATION_JOURNAL_TTL_MS = 24 * 60 * 60 * 1000;
const OPERATION_FINGERPRINT_PATTERN = /^[0-9a-f]{32}$/;
export const UUID_PATTERN =
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export function browserSessionStorage() {
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
    if (!value || typeof value !== "object" || Array.isArray(value)) {
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
