/** @odoo-module **/

import {Component, onWillDestroy, useEffect, useRef, useState} from "@odoo/owl";
import {
    VoiceRecorderSession,
    formatVoiceRecordingDuration,
    voiceRecorderAvailable,
} from "./voice_recorder.esm";
import {
    conversationUiPolicy,
    formatFileSize,
    makeClientRequestId,
    mediaCapabilities,
    mediaCaptionAllowed,
    messagePreviewText,
    normalizeMessageMedia,
    validateMediaFile,
} from "./contact_center_model.esm";
import {
    STRUCTURED_ACTION_LABELS,
    STRUCTURED_CONTENT_LABELS,
    newStructuredDraft,
    newStructuredRow,
    outboundStructuredCapabilities,
    structuredDraftSubmission,
} from "./structured_content.esm";
import {deserializeDateTime} from "@web/core/l10n/dates";
import {session} from "@web/session";
import {useService} from "@web/core/utils/hooks";

const {DateTime} = luxon;

const DRAFT_STORAGE_VERSION = 2;
const DRAFT_STORAGE_PREFIX = "contact_center_ui.composer_drafts";
const COMPOSER_MODES = new Set(["message", "note"]);
const PRODUCTIVITY_TOOLS = new Set(["quick_reply", "followup", "schedule"]);
const MAX_PERSISTED_DRAFTS = 100;
const MAX_DRAFT_BODY_LENGTH = 65536;
const MAX_RETAINED_UPLOAD_PREVIEW_BYTES = 8 * 1024 * 1024;
export const MAX_COMPOSER_ATTACHMENTS = 10;
// SessionStorage quotas vary by browser and count UTF-16 storage. Keep the
// newest complete drafts inside a bounded budget instead of silently clipping
// every draft to a lower limit than the server accepts.
const MAX_PERSISTED_DRAFT_CHARACTERS = 500000;

const ACCEPT_BY_KIND = Object.freeze({
    image: "image/*",
    audio: "audio/*",
    video: "video/*",
});

function browserSessionStorage() {
    try {
        return window.sessionStorage;
    } catch (_error) {
        return false;
    }
}

export function composerDraftStorageKey(database, userId) {
    const cleanDatabase =
        typeof database === "string" ? database.trim().slice(0, 160) : "";
    const cleanUserId = Number(userId);
    if (!cleanDatabase || !Number.isSafeInteger(cleanUserId) || cleanUserId <= 0) {
        return false;
    }
    return `${DRAFT_STORAGE_PREFIX}.v${DRAFT_STORAGE_VERSION}.${encodeURIComponent(
        cleanDatabase
    )}.${cleanUserId}`;
}

function sanitizedPersistedDraft(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        return false;
    }
    const body = typeof value.body === "string" ? value.body : "";
    const mode = COMPOSER_MODES.has(value.mode) ? value.mode : "message";
    if (!body.trim()) {
        return false;
    }
    return {body: body.slice(0, MAX_DRAFT_BODY_LENGTH), mode};
}

function restoredProductivityDraft(draft) {
    return {
        scheduledAt: draft.scheduledAt || "",
        followupSummary: draft.followupSummary || "",
        followupNote: draft.followupNote || "",
        followupDate: draft.followupDate || "",
        followupActivityTypeId: draft.followupActivityTypeId || false,
        followupUserId: draft.followupUserId || false,
    };
}

function uploadedMediaSize(serverMedia, pendingFile, attachment) {
    if (serverMedia && serverMedia.size_bytes) {
        return serverMedia.size_bytes;
    }
    if (pendingFile && pendingFile.size) {
        return pendingFile.size;
    }
    return (attachment && attachment.size) || 0;
}

function mergedUploadedAttachment(serverMedia, attachment, releasePreview) {
    if (!serverMedia || !attachment) {
        return attachment;
    }
    return {
        ...attachment,
        name: serverMedia.name || attachment.name,
        size: serverMedia.size_bytes || attachment.size,
        mimetype: serverMedia.mimetype || attachment.mimetype,
        kind: serverMedia.kind || attachment.kind,
        isVoiceNote: serverMedia.is_voice_note === true || attachment.isVoiceNote,
        durationSeconds: serverMedia.duration_seconds || attachment.durationSeconds,
        previewUrl: releasePreview ? "" : attachment.previewUrl,
    };
}

export function buildAttachmentSendPlan(
    body,
    attachments,
    capabilities,
    replyId = false
) {
    const text = typeof body === "string" ? body.trim() : "";
    const captionIndex = text
        ? attachments.findIndex((item) => mediaCaptionAllowed(capabilities, item.kind))
        : -1;
    const plan = [];
    if (text && captionIndex === -1) {
        plan.push({body: text, mediaRef: "", attachmentId: false});
    }
    attachments.forEach((item, index) => {
        plan.push({
            body: index === captionIndex ? text : "",
            mediaRef: item.mediaRef,
            attachmentId: item.id,
        });
    });
    return plan.map((operation, index) => ({
        ...operation,
        replyId: index === 0 ? replyId : false,
    }));
}

export async function admitAttachmentSequence(plan, {isActive, send, onAdmitted}) {
    let admitted = 0;
    while (plan.length && isActive()) {
        const operation = plan[0];
        const outcome = await send(operation);
        const accepted =
            typeof outcome === "object" && outcome ? outcome.accepted : outcome;
        if (!accepted) {
            if (outcome && outcome.requestId) {
                operation.requestId = outcome.requestId;
            }
            const definitive = Boolean(
                outcome && outcome.definitive && !operation.uncertain
            );
            if (!definitive) {
                operation.uncertain = true;
            }
            return {complete: false, admitted, failed: true, definitive};
        }
        // Remove an acknowledged operation before touching presentation state.
        // A view refresh or disposal can never make it eligible for another send.
        plan.shift();
        admitted += 1;
        onAdmitted(operation);
    }
    return {complete: !plan.length, admitted, failed: false};
}

export function readComposerDrafts(storage, key) {
    const drafts = new Map();
    if (!storage || !key) {
        return drafts;
    }
    try {
        const parsed = JSON.parse(storage.getItem(key) || "{}");
        const records =
            parsed &&
            parsed.version === DRAFT_STORAGE_VERSION &&
            Array.isArray(parsed.drafts)
                ? parsed.drafts
                : [];
        for (const value of records.slice(-MAX_PERSISTED_DRAFTS)) {
            const channelId = Number(value && value.channel_id);
            const draft = sanitizedPersistedDraft(value);
            if (Number.isSafeInteger(channelId) && channelId > 0 && draft) {
                drafts.delete(channelId);
                drafts.set(channelId, draft);
            }
        }
    } catch (_error) {
        return new Map();
    }
    return drafts;
}

export function writeComposerDrafts(storage, key, drafts) {
    if (!storage || !key || !(drafts instanceof Map)) {
        return false;
    }
    const records = [];
    let persistedCharacters = 0;
    const candidates = [...drafts.entries()].slice(-MAX_PERSISTED_DRAFTS).reverse();
    for (const [channelId, value] of candidates) {
        const draft = sanitizedPersistedDraft(value);
        const bodyLength = draft ? draft.body.length : 0;
        if (
            Number.isSafeInteger(channelId) &&
            channelId > 0 &&
            draft &&
            persistedCharacters + bodyLength <= MAX_PERSISTED_DRAFT_CHARACTERS
        ) {
            records.push({channel_id: channelId, ...draft});
            persistedCharacters += bodyLength;
        }
    }
    try {
        if (!records.length) {
            storage.removeItem(key);
            return true;
        }
        storage.setItem(
            key,
            JSON.stringify({
                version: DRAFT_STORAGE_VERSION,
                drafts: records.reverse(),
            })
        );
        return true;
    } catch (_error) {
        return false;
    }
}

function twoDigit(value) {
    return String(value).padStart(2, "0");
}

export function localDateTimeInputValue(value) {
    const date = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(date.getTime())) {
        return "";
    }
    return `${date.getFullYear()}-${twoDigit(date.getMonth() + 1)}-${twoDigit(
        date.getDate()
    )}T${twoDigit(date.getHours())}:${twoDigit(date.getMinutes())}`;
}

export function scheduledMessageStateMeta(state) {
    return (
        {
            scheduled: {
                key: "scheduled",
                label: "Agendada",
                icon: "fa fa-clock-o",
            },
            processing: {
                key: "processing",
                label: "Enviando",
                icon: "fa fa-circle-o-notch fa-spin",
            },
            released: {
                key: "released",
                label: "Enviada",
                icon: "fa fa-check",
            },
            cancelled: {
                key: "cancelled",
                label: "Cancelada",
                icon: "fa fa-ban",
            },
            failed: {
                key: "failed",
                label: "Falhou",
                icon: "fa fa-exclamation-triangle",
            },
        }[state] || {
            key: "unknown",
            label: "Estado desconhecido",
            icon: "fa fa-question-circle",
        }
    );
}

function editableComposerShortcutTarget(target) {
    if (!target) {
        return false;
    }
    const tagName = String(target.tagName || "").toLowerCase();
    if (["input", "textarea", "select"].includes(tagName)) {
        return true;
    }
    if (target.isContentEditable === true) {
        return true;
    }
    return Boolean(
        typeof target.closest === "function" &&
            target.closest("[contenteditable='true']")
    );
}

export function composerShortcut(event) {
    if (!event) {
        return false;
    }
    const hasPrimaryModifier = [event.ctrlKey, event.metaKey].some(Boolean);
    if (
        [event.isComposing, event.altKey, !event.shiftKey, !hasPrimaryModifier].some(
            Boolean
        ) ||
        editableComposerShortcutTarget(event.target)
    ) {
        return false;
    }
    return (
        {
            q: "quick_reply",
            n: "note",
            f: "followup",
            g: "schedule",
        }[String(event.key || "").toLowerCase()] || false
    );
}

export function transferredFiles(transfer) {
    if (!transfer) {
        return [];
    }
    if (transfer.files && transfer.files.length) {
        return Array.from(transfer.files).filter(Boolean);
    }
    return Array.from(transfer.items || [])
        .filter((item) => item && item.kind === "file")
        .map((item) =>
            typeof item.getAsFile === "function" ? item.getAsFile() : false
        )
        .filter(Boolean);
}

function transferCarriesFiles(transfer) {
    return transferredFiles(transfer).length > 0;
}

export function composerHint(conversation, bootstrap) {
    const user = bootstrap && bootstrap.user;
    const account = conversation && conversation.account;
    const signedBy =
        account &&
        account.signature_enabled === true &&
        conversation.capabilities &&
        conversation.capabilities.sender_signature === true &&
        user &&
        typeof user.name === "string"
            ? user.name.trim()
            : "";
    return [
        signedBy ? `Assinada como ${signedBy}` : "",
        "Enter envia",
        "Shift+Enter quebra linha",
    ]
        .filter(Boolean)
        .join(" · ");
}

export class MessageComposer extends Component {
    setup() {
        this.action = useService("action");
        this.inputRef = useRef("input");
        this.fileRef = useRef("file");
        this.voiceCancelRef = useRef("voiceCancel");
        this.toolSearchRef = useRef("toolSearch");
        this.local = useState({
            body: "",
            mode: "message",
            tool: false,
            actionBusy: false,
            quickReplyQuery: "",
            scheduledAt: "",
            followupSummary: "",
            followupNote: "",
            followupDate: "",
            followupActivityTypeId: false,
            followupUserId: false,
            completionFeedback: {},
            attachments: [],
            structuredDraft: false,
            uploadError: "",
            sendPlan: false,
            sendStatus: "",
            recordingPhase: "idle",
            recordingElapsedMs: 0,
            recordingError: "",
            dragActive: false,
        });
        this.uploadGeneration = 0;
        this.uploadControllers = new Map();
        this.destroyed = false;
        this.voiceCaptureHadFocus = false;
        this.drafts = new Map();
        this.draftStorage = Object.prototype.hasOwnProperty.call(
            this.props,
            "draftStorage"
        )
            ? this.props.draftStorage
            : browserSessionStorage();
        this.draftStorageKey = composerDraftStorageKey(
            this.props.draftDatabase ||
                session.db ||
                session.db_name ||
                (window.location && window.location.host) ||
                "",
            this.props.state.bootstrap && this.props.state.bootstrap.user
                ? this.props.state.bootstrap.user.id
                : false
        );
        this.persistedDrafts = readComposerDrafts(
            this.draftStorage,
            this.draftStorageKey
        );
        this.activeChannelId = false;
        this.switchPrepared = false;
        this.unregisterConversationGuard =
            this.store.registerConversationSelectionGuard((channelId, options) =>
                this.prepareConversationSwitch(channelId, options)
            );
        this.unregisterDraftAttachmentHandler =
            this.store.registerDraftAttachmentHandler((source) =>
                this.addDraftAttachment(source)
            );
        this.voiceRecorder = new VoiceRecorderSession({
            scope: window,
            onPhase: (phase) => {
                this.local.recordingPhase = phase;
            },
            onTick: (elapsedMs) => {
                this.local.recordingElapsedMs = elapsedMs;
            },
            onComplete: (recording) => this.onVoiceRecordingComplete(recording),
            onError: (error) => {
                this.local.recordingError = error;
            },
        });
        this.activateConversation(this.props.state.selectedChannelId);
        this.onWindowKeydown = (event) => this.onShortcut(event);
        window.addEventListener("keydown", this.onWindowKeydown);
        useEffect(
            () => {
                this.activateConversation(this.props.state.selectedChannelId);
                this.resize();
            },
            () => [this.props.state.selectedChannelId]
        );
        useEffect(
            () => {
                if (!this.canAttach) {
                    this.cancelVoiceRecording();
                    if (!this.local.sendPlan) {
                        this.clearLocalAttachments();
                    }
                }
            },
            () => [this.canAttach]
        );
        useEffect(
            () => {
                const active = this.voiceCaptureActive;
                if (active && !this.voiceCaptureHadFocus) {
                    this.voiceCaptureHadFocus = true;
                    if (this.voiceCancelRef.el) {
                        this.voiceCancelRef.el.focus();
                    }
                } else if (!active && this.voiceCaptureHadFocus) {
                    this.voiceCaptureHadFocus = false;
                    if (this.inputRef.el) {
                        this.inputRef.el.focus();
                    }
                }
            },
            () => [this.local.recordingPhase]
        );
        onWillDestroy(() => {
            this.destroyed = true;
            this.saveCurrentDraft();
            window.removeEventListener("keydown", this.onWindowKeydown);
            if (this.unregisterConversationGuard) {
                this.unregisterConversationGuard();
            }
            this.unregisterDraftAttachmentHandler();
            this.voiceRecorder.destroy();
            this.disposeDrafts();
        });
    }

    get store() {
        return this.props.store;
    }

    get state() {
        return this.props.state;
    }

    get conversation() {
        return this.props.conversation;
    }

    get sending() {
        return this.store && typeof this.store.isSending === "function"
            ? this.store.isSending(this.conversation && this.conversation.channel_id)
            : Boolean(this.state.sending);
    }

    get capabilities() {
        return (this.conversation && this.conversation.capabilities) || {};
    }

    get applicationCapabilities() {
        return this.store.capabilities || {};
    }

    get noteMode() {
        return this.local.mode === "note";
    }

    get scheduleMode() {
        return this.local.tool === "schedule";
    }

    get productivity() {
        return (
            this.state.productivity || {
                phase: "idle",
                activities: [],
                scheduledMessages: [],
                activityTypes: [],
                assignableUsers: [],
                error: "",
            }
        );
    }

    get quickReplies() {
        return (
            this.state.quickReplies || {
                phase: "idle",
                items: [],
                error: "",
            }
        );
    }

    get minimumScheduledAt() {
        return localDateTimeInputValue(new Date(Date.now() + 60 * 1000));
    }

    get maximumScheduledAt() {
        return localDateTimeInputValue(
            new Date(Date.now() + 365 * 24 * 60 * 60 * 1000)
        );
    }

    get minimumFollowupDate() {
        return localDateTimeInputValue(new Date()).slice(0, 10);
    }

    get canUseQuickReplies() {
        return this.applicationCapabilities.quick_replies === true;
    }

    get canUseInternalNotes() {
        return this.applicationCapabilities.internal_notes === true;
    }

    get canUseFollowups() {
        return this.applicationCapabilities.followups === true;
    }

    get canUseScheduledMessages() {
        return this.applicationCapabilities.scheduled_messages === true;
    }

    get canCreateScheduledMessage() {
        return Boolean(
            this.canUseScheduledMessages && this.conversationPolicy.allow_send === true
        );
    }

    get scheduledTextOnlyAllowed() {
        return [
            this.canCreateScheduledMessage,
            !this.noteMode,
            !this.attachments.length,
            !this.local.sendPlan,
            !this.local.structuredDraft,
            !this.state.replyTo,
            !this.voiceCaptureActive,
            !this.uploadInProgress,
        ].every(Boolean);
    }

    get canScheduleCurrentMessage() {
        return Boolean(
            this.scheduledTextOnlyAllowed &&
                !this.local.actionBusy &&
                this.local.body.trim() &&
                this.local.scheduledAt
        );
    }

    get hasProductivityToolbar() {
        return Boolean(
            this.structuredTypes.length ||
                this.local.structuredDraft ||
                this.canUseQuickReplies ||
                this.canUseInternalNotes ||
                this.canUseFollowups ||
                this.canUseScheduledMessages
        );
    }

    get conversationPolicy() {
        return conversationUiPolicy(this.conversation);
    }

    get canAttach() {
        return (
            !this.noteMode &&
            !this.scheduleMode &&
            !this.local.structuredDraft &&
            this.conversationPolicy.allow_attachments
        );
    }

    get attachments() {
        return this.local.attachments || [];
    }

    get structuredTypes() {
        return Object.keys(this.structuredCapabilities);
    }

    get structuredCapabilities() {
        return outboundStructuredCapabilities(this.capabilities);
    }

    get structuredSpec() {
        const draft = this.local.structuredDraft;
        return draft && this.structuredTypes.includes(draft.type)
            ? this.structuredCapabilities[draft.type]
            : false;
    }

    get structuredActions() {
        return this.structuredSpec && this.local.structuredDraft.type === "buttons"
            ? this.structuredSpec.action_types
            : [];
    }

    structuredActionLabel(action) {
        return STRUCTURED_ACTION_LABELS[action] || "Ação indisponível";
    }

    get structuredRowTitleLimit() {
        if (!this.structuredSpec) {
            return 0;
        }
        return this.local.structuredDraft.type === "buttons"
            ? this.structuredSpec.max_button_title_length
            : this.structuredSpec.max_row_title_length;
    }

    get structuredRowLimit() {
        if (!this.structuredSpec) {
            return 0;
        }
        return this.local.structuredDraft.type === "buttons"
            ? this.structuredSpec.max_buttons
            : this.structuredSpec.max_rows;
    }

    get canAddStructuredRow() {
        return Boolean(
            this.canChooseStructured &&
                this.structuredSpec &&
                this.local.structuredDraft.rows.length < this.structuredRowLimit
        );
    }

    get bodyMaxLength() {
        return this.structuredSpec && this.structuredSpec.body_mode !== "none"
            ? this.structuredSpec.max_body_length * 2
            : 65536;
    }

    get structuredBodyHint() {
        const spec = this.structuredSpec;
        if (!spec) {
            return "";
        }
        return spec.body_mode === "none"
            ? "Este cartão não aceita texto adicional."
            : `Texto ${
                  spec.body_mode === "required" ? "obrigatório" : "opcional"
              }, até ${spec.max_body_length} caracteres.`;
    }

    structuredLabel(type) {
        return STRUCTURED_CONTENT_LABELS[type];
    }

    get structuredSubmission() {
        return structuredDraftSubmission(
            this.local.structuredDraft,
            this.local.body,
            this.capabilities
        );
    }

    get canChooseStructured() {
        return (
            !this.noteMode &&
            !this.scheduleMode &&
            this.conversationPolicy.allow_send &&
            !this.attachments.length &&
            !this.voiceCaptureActive &&
            !this.local.sendPlan &&
            !this.local.actionBusy &&
            !this.sending
        );
    }

    onStructuredType(event) {
        if (this.canChooseStructured) {
            const type = event.target.value;
            if (!type) {
                this.local.structuredDraft = false;
            } else if (this.structuredTypes.includes(type)) {
                this.local.structuredDraft = newStructuredDraft(
                    type,
                    this.capabilities
                );
            }
        }
    }

    addStructuredRow() {
        const draft = this.local.structuredDraft;
        if (this.canAddStructuredRow) {
            draft.rows.push(
                newStructuredRow(
                    draft.type === "buttons" ? this.structuredActions[0] : "reply",
                    this.structuredSpec.max_id_length,
                    draft.rows
                )
            );
        }
    }

    removeStructuredRow(row) {
        const draft = this.local.structuredDraft;
        if (this.canChooseStructured && draft && draft.rows.length > 1) {
            draft.rows = draft.rows.filter((item) => item.id !== row.id);
        }
    }

    get uploadInProgress() {
        return this.attachments.some((item) =>
            ["pending", "uploading"].includes(item.phase)
        );
    }

    get canChooseAttachments() {
        return Boolean(
            this.canAttach &&
                !this.local.sendPlan &&
                !this.local.actionBusy &&
                !this.voiceCaptureActive &&
                !this.uploadInProgress &&
                !this.sending &&
                this.attachments.length < MAX_COMPOSER_ATTACHMENTS
        );
    }

    get attachmentCaptionHint() {
        if (!this.attachments.length) {
            return "";
        }
        const compatible = this.attachments.find((item) =>
            mediaCaptionAllowed(this.capabilities, item.kind)
        );
        return compatible
            ? `O texto será a legenda de ${compatible.name}. Cada arquivo será enviado separadamente.`
            : "O texto será enviado uma vez, antes dos arquivos. Cada arquivo será enviado separadamente.";
    }

    get voiceRecordingAvailable() {
        return voiceRecorderAvailable(window, this.voiceRecordingPolicy);
    }

    get voiceRecordingPolicy() {
        return mediaCapabilities(this.capabilities).audio || false;
    }

    get voiceCaptureActive() {
        return ["requesting", "recording", "processing"].includes(
            this.local.recordingPhase
        );
    }

    get switchHasBlockingWork() {
        return Boolean(
            this.voiceCaptureActive || this.uploadInProgress || this.local.actionBusy
        );
    }

    prepareConversationSwitch(channelId, {checkOnly = false} = {}) {
        if (checkOnly) {
            // Starting a channel checks before its ID exists. Do not mark the
            // composer prepared or save/change the draft before that RPC.
            return !this.switchHasBlockingWork;
        }
        if (!channelId || channelId === this.activeChannelId || this.switchPrepared) {
            return true;
        }
        if (this.switchHasBlockingWork) {
            this.store.notify(
                "Conclua ou cancele a ação em andamento antes de trocar de conversa.",
                {type: "warning", title: "Trabalho em andamento"}
            );
            return false;
        }
        this.saveCurrentDraft();
        this.switchPrepared = true;
        return true;
    }

    draftHasContent(draft) {
        return Boolean(
            draft &&
                (draft.body.trim() ||
                    (draft.attachments && draft.attachments.length) ||
                    (draft.sendPlan && draft.sendPlan.length) ||
                    draft.structuredDraft ||
                    draft.replyTo ||
                    draft.scheduledAt ||
                    draft.followupSummary ||
                    draft.followupNote ||
                    draft.followupDate)
        );
    }

    persistDraft(channelId, body = this.local.body, mode = this.local.mode) {
        if (
            !Number.isSafeInteger(channelId) ||
            channelId <= 0 ||
            !this.persistedDrafts
        ) {
            return false;
        }
        const persisted = sanitizedPersistedDraft({body, mode});
        if (persisted) {
            // Map preserves insertion order. Move an updated draft to the end so
            // quota pruning always favors the conversations edited most recently.
            this.persistedDrafts.delete(channelId);
            this.persistedDrafts.set(channelId, persisted);
        } else {
            this.persistedDrafts.delete(channelId);
        }
        return writeComposerDrafts(
            this.draftStorage,
            this.draftStorageKey,
            this.persistedDrafts
        );
    }

    clearPersistedDraft(channelId) {
        if (this.persistedDrafts) {
            this.persistedDrafts.delete(channelId);
            writeComposerDrafts(
                this.draftStorage,
                this.draftStorageKey,
                this.persistedDrafts
            );
        }
    }

    saveCurrentDraft() {
        const channelId = this.activeChannelId;
        if (!Number.isSafeInteger(channelId) || channelId <= 0) {
            return false;
        }
        const draft = {
            body: this.local.body,
            mode: this.local.mode,
            attachments: this.attachments,
            structuredDraft: this.local.structuredDraft,
            uploadError: this.local.uploadError,
            sendPlan: this.local.sendPlan,
            sendStatus: this.local.sendStatus,
            replyTo: this.state.replyTo ? {...this.state.replyTo} : false,
            scheduledAt: this.local.scheduledAt,
            followupSummary: this.local.followupSummary,
            followupNote: this.local.followupNote,
            followupDate: this.local.followupDate,
            followupActivityTypeId: this.local.followupActivityTypeId,
            followupUserId: this.local.followupUserId,
        };
        if (this.draftHasContent(draft)) {
            this.drafts.set(channelId, draft);
            this.persistDraft(channelId, draft.body, draft.mode);
            return true;
        }
        this.drafts.delete(channelId);
        this.clearPersistedDraft(channelId);
        return false;
    }

    clearLocalAttachments({revoke = true} = {}) {
        this.uploadGeneration += 1;
        if (this.uploadControllers.has("draft-document")) {
            this.local.actionBusy = false;
        }
        for (const controller of this.uploadControllers.values()) {
            controller.abort();
        }
        this.uploadControllers.clear();
        if (revoke) {
            this.attachments.forEach((item) => this.revokePreview(item));
        }
        this.local.attachments = [];
        this.local.uploadError = "";
        if (this.fileRef.el) {
            this.fileRef.el.value = "";
        }
    }

    resetLocalForConversation({revoke = true} = {}) {
        this.cancelVoiceRecording();
        this.clearLocalAttachments({revoke});
        this.local.sendPlan = false;
        this.local.structuredDraft = false;
        this.local.sendStatus = "";
        this.local.actionBusy = false;
        this.local.body = "";
        this.local.mode = "message";
        this.local.tool = false;
        this.local.scheduledAt = "";
        this.local.followupSummary = "";
        this.local.followupNote = "";
        this.local.followupDate = "";
        this.local.followupActivityTypeId = false;
        this.local.followupUserId = false;
        this.local.completionFeedback = {};
        this.local.recordingError = "";
        this.local.dragActive = false;
    }

    restoreDraft(channelId) {
        const draft = this.drafts.get(channelId) || this.persistedDrafts.get(channelId);
        this.drafts.delete(channelId);
        if (!draft) {
            this.state.replyTo = false;
            return false;
        }
        this.local.body = draft.body;
        this.local.mode = COMPOSER_MODES.has(draft.mode) ? draft.mode : "message";
        this.local.attachments = draft.attachments || [];
        this.local.structuredDraft = draft.structuredDraft || false;
        this.local.uploadError = draft.uploadError || "";
        this.local.sendPlan =
            draft.sendPlan && draft.sendPlan.length ? draft.sendPlan : false;
        this.local.sendStatus = draft.sendStatus || "";
        this.state.replyTo = draft.replyTo || false;
        Object.assign(this.local, restoredProductivityDraft(draft));
        this.resize();
        return true;
    }

    activateConversation(channelId) {
        if (channelId === this.activeChannelId) {
            return;
        }
        if (this.activeChannelId) {
            // Direct clicks pass through the store guard. Filters, access changes
            // and list reconciliation may change the selected channel without
            // using that path, so persist once more before resetting local state.
            if (!this.switchPrepared) {
                if (this.uploadInProgress) {
                    for (const item of this.attachments) {
                        if (["pending", "uploading"].includes(item.phase)) {
                            item.phase = "error";
                            item.error = "Upload interrompido. Tente novamente.";
                        }
                    }
                }
                this.saveCurrentDraft();
            }
            this.resetLocalForConversation({revoke: false});
        }
        this.activeChannelId = channelId || false;
        this.switchPrepared = false;
        if (channelId) {
            this.restoreDraft(channelId);
        }
    }

    revokeDraftPreview(draft) {
        for (const item of (draft && draft.attachments) || []) {
            this.revokePreview(item);
        }
    }

    disposeDrafts() {
        this.revokeDraftPreview({attachments: this.attachments});
        for (const draft of this.drafts.values()) {
            this.revokeDraftPreview(draft);
        }
        this.drafts.clear();
        this.clearLocalAttachments({revoke: false});
    }

    get canStartVoiceRecording() {
        return Boolean(
            this.voiceRecordingAvailable &&
                this.conversationPolicy.allow_send &&
                this.canAttach &&
                !this.local.body.trim() &&
                !this.attachments.length &&
                !this.local.sendPlan &&
                !this.uploadInProgress &&
                this.local.recordingPhase === "idle" &&
                (!this.state.replyTo || this.conversationPolicy.allow_reply) &&
                !this.sending
        );
    }

    get showVoiceRecordingButton() {
        return Boolean(
            !this.noteMode &&
                !this.scheduleMode &&
                this.voiceRecordingAvailable &&
                !this.local.structuredDraft &&
                !this.local.sendPlan &&
                !this.local.body.trim() &&
                !this.attachments.length &&
                !this.voiceCaptureActive
        );
    }

    get voiceRecordingTime() {
        return formatVoiceRecordingDuration(this.local.recordingElapsedMs / 1000);
    }

    get voiceRecordingStatus() {
        return (
            {
                requesting: "Ativando microfone…",
                recording: "Gravando áudio",
                processing: "Preparando áudio…",
            }[this.local.recordingPhase] || ""
        );
    }

    get voiceRecordingLimit() {
        const providerLimit = this.voiceRecordingPolicy.max_duration_seconds;
        const seconds =
            Number.isSafeInteger(providerLimit) && providerLimit > 0
                ? Math.min(15 * 60, providerLimit)
                : 15 * 60;
        return `Limite: ${formatVoiceRecordingDuration(seconds)}`;
    }

    replyAuthorLabel() {
        const author = this.state.replyTo && this.state.replyTo.author;
        return author && typeof author.name === "string" && author.name.trim()
            ? author.name.trim()
            : "Mensagem";
    }

    get composerHint() {
        if (this.noteMode) {
            return "Visível somente para a equipe · Enter registra";
        }
        if (this.scheduleMode) {
            return "Enter agenda · Shift+Enter quebra linha";
        }
        return composerHint(this.conversation, this.state.bootstrap);
    }

    get acceptedFiles() {
        const kinds = Object.keys(mediaCapabilities(this.capabilities));
        if (kinds.includes("document")) {
            return "";
        }
        return kinds
            .map((kind) => ACCEPT_BY_KIND[kind])
            .filter(Boolean)
            .join(",");
    }

    get canSendInternalNote() {
        return [
            Boolean(this.conversation),
            this.canUseInternalNotes,
            Boolean(this.local.body.trim()),
            !this.attachments.length,
            !this.local.structuredDraft,
            !this.state.replyTo,
            !this.voiceCaptureActive,
            !this.local.actionBusy,
        ].every(Boolean);
    }

    get canSendExternalMessage() {
        return [
            this.conversationPolicy.allow_send,
            Boolean(
                this.local.body.trim() ||
                    this.attachments.length ||
                    this.local.sendPlan ||
                    this.local.structuredDraft
            ),
            this.canReplayPendingSend ||
                !this.local.structuredDraft ||
                Boolean(this.structuredSubmission.content),
            this.canReplayPendingSend || !this.attachments.length || this.canAttach,
            this.local.sendPlan ||
                !this.state.replyTo ||
                this.conversationPolicy.allow_reply,
            this.attachments.every((item) => item.phase === "ready" && item.mediaRef),
            !this.uploadInProgress,
            !this.voiceCaptureActive,
            !this.sending,
            !this.local.actionBusy,
        ].every(Boolean);
    }

    get canReplayPendingSend() {
        const operation = this.local.sendPlan && this.local.sendPlan[0];
        return Boolean(operation && operation.uncertain && operation.requestId);
    }

    get canSend() {
        if (this.scheduleMode) {
            return false;
        }
        return this.noteMode ? this.canSendInternalNote : this.canSendExternalMessage;
    }

    get primaryActionAvailable() {
        return this.scheduleMode ? this.canScheduleCurrentMessage : this.canSend;
    }

    get primaryActionTitle() {
        if (this.scheduleMode) {
            return this.canScheduleCurrentMessage
                ? "Agendar mensagem"
                : "Informe somente texto, data e hora para agendar";
        }
        if (this.canSend) {
            return this.noteMode ? "Registrar nota interna" : "Enviar mensagem";
        }
        return this.disabledReason;
    }

    get disabledReason() {
        if (!this.conversation) {
            return "Selecione uma conversa";
        }
        if (this.noteMode && !this.canUseInternalNotes) {
            return "Notas internas não estão disponíveis";
        }
        if (this.noteMode && !this.local.body.trim()) {
            return "Escreva uma nota interna";
        }
        if (this.conversationPolicy.is_group && !this.conversationPolicy.allow_send) {
            return "O envio para este grupo não está habilitado";
        }
        if (!this.conversation.can_send) {
            return "O provider desta conversa não está disponível para envio";
        }
        if (this.uploadInProgress) {
            return "Aguarde o preparo dos arquivos";
        }
        if (this.attachments.some((item) => item.phase === "error")) {
            return "Tente novamente ou remova os arquivos com erro";
        }
        if (this.structuredSubmission.error) {
            return this.structuredSubmission.error;
        }
        if (this.voiceCaptureActive) {
            return "Conclua ou cancele a gravação de áudio";
        }
        return "Escreva uma mensagem";
    }

    attachmentLabel(attachment) {
        if (!attachment) {
            return "";
        }
        const labels = {
            image: "Imagem",
            audio: attachment.isVoiceNote
                ? "Mensagem de voz"
                : attachment.isRecordedAudio
                ? "Gravação de áudio"
                : "Áudio",
            video: "Vídeo",
            document: "Documento",
        };
        return [
            labels[attachment.kind] || "Arquivo",
            attachment.durationSeconds
                ? formatVoiceRecordingDuration(attachment.durationSeconds)
                : "",
            formatFileSize(attachment.size),
        ]
            .filter(Boolean)
            .join(" · ");
    }

    replyPreview() {
        const current =
            this.state.replyTo &&
            this.state.messages.find(
                (message) => message.message_id === this.state.replyTo.message_id
            );
        return messagePreviewText(current || this.state.replyTo);
    }

    focusInput() {
        window.requestAnimationFrame(() => {
            if (this.inputRef.el) {
                this.inputRef.el.focus();
            }
        });
    }

    setComposerMode(mode) {
        if (!COMPOSER_MODES.has(mode)) {
            return false;
        }
        if (mode === "note") {
            if (!this.canUseInternalNotes) {
                return false;
            }
            if (
                this.voiceCaptureActive ||
                this.uploadInProgress ||
                this.local.sendPlan
            ) {
                this.store.notify(
                    "Conclua ou cancele o áudio ou arquivo antes de criar uma nota.",
                    {type: "warning", title: "Trabalho em andamento"}
                );
                return false;
            }
            if (this.attachments.length || this.local.structuredDraft) {
                this.store.notify(
                    "Remova os anexos ou o cartão antes de criar uma nota interna.",
                    {
                        type: "warning",
                        title: "Nota somente em texto",
                    }
                );
                return false;
            }
            this.store.clearReply();
        }
        this.local.mode = mode;
        this.local.tool = false;
        this.persistDraft(this.activeChannelId);
        this.focusInput();
        return true;
    }

    toggleNoteMode() {
        return this.setComposerMode(this.noteMode ? "message" : "note");
    }

    closeTool() {
        this.local.tool = false;
        this.focusInput();
    }

    initializeProductivityForm() {
        const now = new Date();
        if (!this.local.scheduledAt) {
            this.local.scheduledAt = localDateTimeInputValue(
                new Date(now.getTime() + 15 * 60 * 1000)
            );
        }
        if (!this.local.followupDate) {
            const tomorrow = new Date(now);
            tomorrow.setDate(tomorrow.getDate() + 1);
            this.local.followupDate = localDateTimeInputValue(tomorrow).slice(0, 10);
        }
        if (
            !this.local.followupActivityTypeId &&
            this.productivity.activityTypes.length
        ) {
            this.local.followupActivityTypeId = this.productivity.activityTypes[0].id;
        }
        if (!this.local.followupUserId && this.store.currentUserId) {
            this.local.followupUserId = this.store.currentUserId;
        }
    }

    async openTool(tool) {
        if (
            !PRODUCTIVITY_TOOLS.has(tool) ||
            this.local.actionBusy ||
            this.local.sendPlan
        ) {
            return false;
        }
        if (this.local.tool === tool) {
            this.closeTool();
            return true;
        }
        if (tool === "quick_reply") {
            if (!this.canUseQuickReplies) {
                return false;
            }
            this.local.tool = tool;
            await this.store.searchQuickReplies(this.local.quickReplyQuery);
            window.requestAnimationFrame(() => {
                if (this.toolSearchRef.el) {
                    this.toolSearchRef.el.focus();
                }
            });
            return true;
        }
        if (tool === "followup" && !this.canUseFollowups) {
            return false;
        }
        if (tool === "schedule") {
            if (!this.canUseScheduledMessages) {
                return false;
            }
            if (
                this.noteMode ||
                this.attachments.length ||
                this.local.structuredDraft ||
                this.state.replyTo ||
                this.voiceCaptureActive
            ) {
                this.store.notify(
                    "Agende somente texto, sem resposta, anexo ou nota interna.",
                    {type: "warning", title: "Agendamento em texto"}
                );
                return false;
            }
        }
        this.local.tool = tool;
        const loaded = await this.store.loadProductivity();
        if (loaded) {
            this.initializeProductivityForm();
        }
        return loaded;
    }

    retryActiveTool() {
        if (this.local.tool === "quick_reply") {
            return this.store.searchQuickReplies(this.local.quickReplyQuery);
        }
        return this.store.loadProductivity({force: true});
    }

    manageQuickReplies() {
        if (this.switchHasBlockingWork) {
            return false;
        }
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: "Respostas rápidas",
            res_model: "contact.center.quick.reply.binding",
            views: [
                [false, "list"],
                [false, "form"],
            ],
            target: "new",
        });
    }

    onQuickReplySearch(event) {
        this.local.quickReplyQuery = event.target.value;
        this.store.searchQuickReplies(this.local.quickReplyQuery);
    }

    insertQuickReply(reply) {
        if (
            !reply ||
            typeof reply.body !== "string" ||
            this.local.sendPlan ||
            this.local.actionBusy
        ) {
            return false;
        }
        const input = this.inputRef.el;
        const current = this.local.body;
        const start =
            input && Number.isSafeInteger(input.selectionStart)
                ? input.selectionStart
                : current.length;
        const end =
            input && Number.isSafeInteger(input.selectionEnd)
                ? input.selectionEnd
                : start;
        const prefix = current.slice(0, start);
        const suffix = current.slice(end);
        const separator = prefix && !/\s$/.test(prefix) ? " " : "";
        this.local.body = `${prefix}${separator}${reply.body}${suffix}`;
        this.local.tool = false;
        this.persistDraft(this.activeChannelId);
        this.resize();
        this.focusInput();
        return true;
    }

    onFollowupField(name, event) {
        const rawValue = event.target.value;
        this.local[name] = ["followupActivityTypeId", "followupUserId"].includes(name)
            ? Number(rawValue) || false
            : rawValue;
    }

    onScheduledAtInput(event) {
        this.local.scheduledAt = event.target.value;
    }

    scheduledDateLabel(value) {
        if (typeof value !== "string" || !value.trim()) {
            return "";
        }
        try {
            return deserializeDateTime(value).toLocaleString(DateTime.DATETIME_SHORT);
        } catch (_error) {
            return value;
        }
    }

    scheduledStateMeta(state) {
        return scheduledMessageStateMeta(state);
    }

    scheduledErrorLabel(scheduled) {
        const error = scheduled && scheduled.error;
        return error && typeof error.message === "string" ? error.message.trim() : "";
    }

    async createFollowup() {
        if (
            this.local.actionBusy ||
            !this.local.followupSummary.trim() ||
            !this.local.followupDate
        ) {
            this.store.notify("Informe o resumo e a data do follow-up.", {
                type: "warning",
                title: "Follow-up incompleto",
            });
            return false;
        }
        const values = {
            summary: this.local.followupSummary.trim(),
            note: this.local.followupNote.trim(),
            date_deadline: this.local.followupDate,
        };
        if (this.local.followupActivityTypeId) {
            values.activity_type_id = this.local.followupActivityTypeId;
        }
        if (this.local.followupUserId) {
            values.user_id = this.local.followupUserId;
        }
        this.local.actionBusy = true;
        let created = false;
        try {
            created = await this.store.scheduleFollowup(values);
        } finally {
            this.local.actionBusy = false;
        }
        if (created) {
            this.local.followupSummary = "";
            this.local.followupNote = "";
        }
        return created;
    }

    onCompletionFeedback(activityId, event) {
        this.local.completionFeedback[activityId] = event.target.value;
    }

    async completeFollowup(activityId) {
        if (this.local.actionBusy) {
            return false;
        }
        this.local.actionBusy = true;
        let completed = false;
        try {
            completed = await this.store.completeFollowup(
                activityId,
                this.local.completionFeedback[activityId] || ""
            );
        } finally {
            this.local.actionBusy = false;
        }
        if (completed) {
            delete this.local.completionFeedback[activityId];
        }
        return completed;
    }

    async scheduleCurrentMessage() {
        if (
            this.local.actionBusy ||
            !this.scheduledTextOnlyAllowed ||
            !this.local.body.trim() ||
            !this.local.scheduledAt
        ) {
            let message = "Informe a mensagem e quando ela deve ser enviada.";
            if (!this.canCreateScheduledMessage) {
                message = "Esta conversa não está disponível para envio externo.";
            } else if (!this.scheduledTextOnlyAllowed) {
                message =
                    "O agendamento aceita somente texto, sem resposta, anexo ou áudio.";
            }
            this.store.notify(message, {
                type: "warning",
                title: "Agendamento incompleto",
            });
            return false;
        }
        const channelId = this.activeChannelId;
        const body = this.local.body;
        this.local.actionBusy = true;
        let scheduled = false;
        try {
            scheduled = await this.store.scheduleMessage(body, this.local.scheduledAt);
        } finally {
            this.local.actionBusy = false;
        }
        if (
            scheduled &&
            this.activeChannelId === channelId &&
            this.local.body === body
        ) {
            this.local.body = "";
            this.clearPersistedDraft(channelId);
            this.resize();
        }
        return scheduled;
    }

    async cancelScheduledMessage(messageId) {
        if (this.local.actionBusy) {
            return false;
        }
        this.local.actionBusy = true;
        try {
            return await this.store.cancelScheduledMessage(messageId);
        } finally {
            this.local.actionBusy = false;
        }
    }

    onShortcut(event) {
        if (event.defaultPrevented || document.querySelector(".o_dialog")) {
            return false;
        }
        const shortcut = composerShortcut(event);
        if (!shortcut || !this.conversation || this.switchHasBlockingWork) {
            return false;
        }
        const available =
            (shortcut === "quick_reply" && this.canUseQuickReplies) ||
            (shortcut === "note" && this.canUseInternalNotes) ||
            (shortcut === "followup" && this.canUseFollowups) ||
            (shortcut === "schedule" && this.canUseScheduledMessages);
        if (!available) {
            return false;
        }
        event.preventDefault();
        if (shortcut === "note") {
            this.toggleNoteMode();
        } else {
            this.openTool(shortcut);
        }
        return true;
    }

    onInput(event) {
        if (this.local.sendPlan || this.local.actionBusy) {
            return;
        }
        this.local.body = event.target.value;
        this.persistDraft(this.activeChannelId);
        this.resize();
    }

    onKeydown(event) {
        if (
            event.key === "Enter" &&
            !event.shiftKey &&
            !event.isComposing &&
            !event.ctrlKey &&
            !event.metaKey
        ) {
            event.preventDefault();
            if (this.scheduleMode) {
                this.scheduleCurrentMessage();
            } else {
                this.send();
            }
        }
    }

    runPrimaryAction() {
        return this.scheduleMode ? this.scheduleCurrentMessage() : this.send();
    }

    async send() {
        if (!this.canSend) {
            return;
        }
        const channelId = this.conversation.channel_id;
        const body = this.local.body;
        this.local.actionBusy = true;
        this.local.sendStatus = "";
        try {
            if (this.noteMode) {
                if (await this.store.postInternalNote(body)) {
                    this.acknowledgeSendOperation(channelId, {
                        body,
                        attachmentId: false,
                    });
                }
                return;
            }
            if (!this.local.sendPlan) {
                this.local.sendPlan = this.local.structuredDraft
                    ? [
                          {
                              body: body.trim(),
                              mediaRef: "",
                              attachmentId: false,
                              replyId: this.state.replyTo
                                  ? this.state.replyTo.message_id
                                  : false,
                              structuredContent: this.structuredSubmission.content,
                          },
                      ]
                    : buildAttachmentSendPlan(
                          body,
                          this.attachments,
                          this.capabilities,
                          this.state.replyTo ? this.state.replyTo.message_id : false
                      );
            }
            const plan = this.local.sendPlan;
            const result = await admitAttachmentSequence(plan, {
                isActive: () =>
                    !this.destroyed &&
                    !this.store.destroyed &&
                    this.state.selectedChannelId === channelId,
                send: (operation) =>
                    this.store.sendMessage(
                        operation.body,
                        operation.mediaRef ? [operation.mediaRef] : [],
                        {
                            channelId,
                            replyId: operation.replyId,
                            structuredContent: operation.structuredContent,
                            replayRequestId: operation.uncertain
                                ? operation.requestId
                                : false,
                            returnOutcome: true,
                        }
                    ),
                onAdmitted: (operation) =>
                    this.acknowledgeSendOperation(channelId, operation),
            });
            if (this.activeChannelId === channelId && !this.destroyed) {
                if (result.complete) {
                    this.local.sendPlan = false;
                    this.local.sendStatus = "";
                } else if (result.failed) {
                    if (result.definitive) {
                        this.local.sendPlan = false;
                        this.local.sendStatus =
                            "O envio foi recusado. Revise os itens restantes e tente novamente.";
                    } else {
                        this.local.sendStatus =
                            `Envio interrompido. ${plan.length} item(ns) aguardam confirmação. ` +
                            "Tente novamente; os itens já aceitos não serão repetidos.";
                    }
                }
            }
        } finally {
            if (this.activeChannelId === channelId && !this.destroyed) {
                this.local.actionBusy = false;
                this.persistDraft(channelId);
                this.resize();
            }
        }
    }

    acknowledgeSendOperation(channelId, operation) {
        const active = !this.destroyed && this.activeChannelId === channelId;
        const draft = active ? this.local : this.drafts.get(channelId);
        if (!draft) {
            const persisted =
                this.persistedDrafts && this.persistedDrafts.get(channelId);
            if (
                operation.body &&
                persisted &&
                persisted.body.trim() === operation.body
            ) {
                this.clearPersistedDraft(channelId);
            }
            return;
        }
        if (operation.body && draft.body.trim() === operation.body) {
            draft.body = "";
        }
        if (operation.attachmentId) {
            draft.attachments = (draft.attachments || []).filter((item) => {
                if (item.id !== operation.attachmentId) {
                    return true;
                }
                this.revokePreview(item);
                return false;
            });
        }
        if (operation.structuredContent) {
            draft.structuredDraft = false;
        }
        if (
            !active &&
            draft.replyTo &&
            draft.replyTo.message_id === operation.replyId
        ) {
            draft.replyTo = false;
        }
        if (!active && !this.draftHasContent(draft)) {
            this.drafts.delete(channelId);
        }
        this.persistDraft(channelId, draft.body, draft.mode);
    }

    resize() {
        const input = this.inputRef.el;
        if (!input) {
            return;
        }
        input.style.height = "auto";
        input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
    }

    chooseAttachment() {
        if (this.canChooseAttachments && this.fileRef.el) {
            this.fileRef.el.click();
        }
    }

    previewUrl(file, kind) {
        if (
            !["image", "audio", "video"].includes(kind) ||
            !window.URL ||
            typeof window.URL.createObjectURL !== "function"
        ) {
            return "";
        }
        return window.URL.createObjectURL(file);
    }

    revokePreview(item) {
        const previewUrl = item && item.previewUrl;
        if (
            previewUrl &&
            window.URL &&
            typeof window.URL.revokeObjectURL === "function"
        ) {
            window.URL.revokeObjectURL(previewUrl);
            item.previewUrl = "";
        }
    }

    removeAttachment(item) {
        if (this.local.sendPlan || this.local.actionBusy) {
            return;
        }
        if (!item) {
            this.local.uploadError = "";
            return;
        }
        const controller = this.uploadControllers.get(item.id);
        if (controller) {
            controller.abort();
            this.uploadControllers.delete(item.id);
        }
        this.revokePreview(item);
        item.file = null;
        this.local.attachments = this.attachments.filter(
            (candidate) => candidate.id !== item.id
        );
    }

    uploadIsStale(generation, channelId, item) {
        return (
            this.destroyed ||
            generation !== this.uploadGeneration ||
            channelId !== this.state.selectedChannelId ||
            !this.attachments.some((candidate) => candidate.id === item.id)
        );
    }

    startUploadController(item) {
        const controller =
            typeof window.AbortController === "function"
                ? new window.AbortController()
                : null;
        if (controller) {
            this.uploadControllers.set(item.id, controller);
        }
        return controller;
    }

    applyUploadedMedia(item, payload) {
        const serverMedia = normalizeMessageMedia([payload.media])[0];
        const uploadedSize = uploadedMediaSize(serverMedia, item.file, item);
        const releasePreview = Boolean(
            item.previewUrl && uploadedSize > MAX_RETAINED_UPLOAD_PREVIEW_BYTES
        );
        if (releasePreview) {
            this.revokePreview(item);
        }
        Object.assign(
            item,
            mergedUploadedAttachment(serverMedia, item, releasePreview)
        );
        item.mediaRef = payload.media_ref.trim();
        item.phase = "ready";
        item.error = "";
        // The immutable server reference owns the prepared upload from here on.
        // Keeping the browser File in each conversation draft would retain the
        // complete binary in memory until that draft is sent or discarded.
        item.file = null;
        item.metadata = {};
    }

    async uploadPendingFile(item, generation, channelId) {
        if (this.uploadIsStale(generation, channelId, item)) {
            return;
        }
        const controller = this.startUploadController(item);
        item.phase = "uploading";
        item.error = "";
        try {
            const payload = await this.store.uploadMedia(
                item.file,
                item.id,
                channelId,
                controller ? controller.signal : undefined,
                item.metadata
            );
            if (this.uploadIsStale(generation, channelId, item)) {
                return;
            }
            this.applyUploadedMedia(item, payload);
        } catch (error) {
            if (
                this.uploadIsStale(generation, channelId, item) ||
                (error && error.name === "AbortError")
            ) {
                return;
            }
            item.mediaRef = "";
            item.phase = "error";
            item.error = (error && error.message) || "O arquivo não pôde ser enviado.";
        } finally {
            if (this.uploadControllers.get(item.id) === controller) {
                this.uploadControllers.delete(item.id);
            }
        }
    }

    retryUpload(item) {
        if (
            !item ||
            !item.file ||
            item.phase !== "error" ||
            this.local.sendPlan ||
            this.uploadInProgress ||
            !this.canAttach
        ) {
            return;
        }
        return this.uploadPendingFile(
            item,
            this.uploadGeneration,
            this.conversation.channel_id
        );
    }

    async prepareFileUpload(file, metadata = {}) {
        return this.prepareFilesUpload([file], metadata);
    }

    draftSourceContext() {
        const conversation = this.store.selectedConversation;
        const identity = conversation && conversation.identity;
        const partner = identity && identity.partner;
        const company = partner && partner.company;
        return {
            channelId: conversation && conversation.channel_id,
            identityId: identity && identity.id,
            customerKey: `${partner ? partner.id : 0}:${company ? company.id : 0}`,
        };
    }

    draftAttachmentReason() {
        if (this.noteMode) {
            return "Selecione Mensagem para anexar um arquivo. A nota interna aceita somente texto.";
        }
        if (!this.canAttach) {
            return "O envio de arquivos não está disponível no modo ou canal atual.";
        }
        if (!this.canChooseAttachments) {
            return "Conclua a ação em andamento ou remova um anexo antes de adicionar outro arquivo.";
        }
        return "";
    }

    draftSourceCurrent(context, generation) {
        const live = this.draftSourceContext();
        return (
            !this.destroyed &&
            generation === this.uploadGeneration &&
            context.channelId === this.state.selectedChannelId &&
            context.channelId === live.channelId &&
            context.identityId === live.identityId &&
            context.customerKey === live.customerKey
        );
    }

    validDraftSource(source, context) {
        return Boolean(
            source &&
                source.channelId === context.channelId &&
                source.customerKey === context.customerKey &&
                this.conversation &&
                this.conversation.channel_id === source.channelId &&
                typeof source.name === "string" &&
                source.name.trim() &&
                typeof source.url === "string" &&
                /^\/contact_center\/[a-zA-Z0-9/_-]+$/.test(source.url)
        );
    }

    async fetchDraftFile(source) {
        const controller = this.startUploadController({id: "draft-document"});
        const timeout = window.setTimeout(
            () => controller && controller.abort(),
            60000
        );
        this.local.actionBusy = true;
        try {
            const response = await window.fetch(source.url, {
                credentials: "same-origin",
                redirect: "error",
                cache: "no-store",
                signal: controller ? controller.signal : undefined,
            });
            if (!response.ok) {
                throw new Error("O arquivo não está disponível ou seu acesso mudou.");
            }
            const blob = await response.blob();
            return new window.File([blob], source.name, {
                type: blob.type || source.mimetype || "application/octet-stream",
            });
        } finally {
            window.clearTimeout(timeout);
            if (this.uploadControllers.get("draft-document") === controller) {
                this.uploadControllers.delete("draft-document");
                this.local.actionBusy = false;
            }
        }
    }

    async addDraftAttachment(source) {
        const context = this.draftSourceContext();
        const generation = this.uploadGeneration;
        const current = () => this.draftSourceCurrent(context, generation);
        if (!current() || !this.validDraftSource(source, context)) {
            this.store.notify("Reabra o painel do cliente para selecionar o arquivo.", {
                type: "warning",
            });
            return false;
        }
        const reason = this.draftAttachmentReason();
        if (reason) {
            this.store.notify(reason, {type: "warning"});
            return false;
        }
        let file = null;
        try {
            file = await this.fetchDraftFile(source);
        } catch (_error) {
            if (current()) {
                this.store.notify(
                    "Não foi possível carregar o arquivo. Verifique seu acesso e tente novamente.",
                    {
                        type: "danger",
                    }
                );
            }
            return false;
        }
        if (!current()) {
            return false;
        }
        const changedReason = this.draftAttachmentReason();
        if (changedReason) {
            this.store.notify(changedReason, {type: "warning"});
            return false;
        }
        const previousIds = new Set(this.attachments.map((item) => item.id));
        const uploading = this.prepareFileUpload(file);
        const added = this.attachments.filter((item) => !previousIds.has(item.id));
        const accepted = await uploading;
        if (!current()) {
            this.discardDraftAttachments(added, context.channelId);
            return false;
        }
        const ready =
            accepted &&
            added.length > 0 &&
            added.every((item) => item.phase === "ready" && item.mediaRef);
        if (!ready) {
            this.store.notify(
                this.local.uploadError ||
                    "O upload não foi concluído. Confira o arquivo no rascunho e tente novamente.",
                {
                    type: "warning",
                }
            );
        }
        return Boolean(ready);
    }

    discardDraftAttachments(items, channelId) {
        const ids = new Set(items.map((item) => item.id));
        for (const item of items) {
            this.revokePreview(item);
            item.file = null;
        }
        this.local.attachments = this.attachments.filter((item) => !ids.has(item.id));
        const draft = this.drafts.get(channelId);
        if (draft) {
            draft.attachments = draft.attachments.filter((item) => !ids.has(item.id));
        }
    }

    async prepareFilesUpload(files, metadata = {}) {
        if (
            !this.conversation ||
            !this.canAttach ||
            this.local.sendPlan ||
            this.local.actionBusy
        ) {
            return false;
        }
        if (this.attachments.length + files.length > MAX_COMPOSER_ATTACHMENTS) {
            this.local.uploadError = `Selecione até ${MAX_COMPOSER_ATTACHMENTS} arquivos por envio.`;
            return false;
        }
        const validated = files.map((file) => ({
            file,
            validation: validateMediaFile(file, this.capabilities),
        }));
        const invalid = validated.find((item) => !item.validation.ok);
        if (invalid) {
            this.local.uploadError = `${invalid.file.name}: ${invalid.validation.error}`;
            return false;
        }
        const generation = this.uploadGeneration;
        const channelId = this.conversation.channel_id;
        const items = validated.map(({file, validation}) => ({
            id: makeClientRequestId(window.crypto),
            file,
            mediaRef: "",
            phase: "pending",
            error: "",
            metadata:
                Number.isSafeInteger(metadata.durationSeconds) &&
                metadata.durationSeconds > 0
                    ? {
                          isVoiceNote: metadata.isVoiceNote === true,
                          durationSeconds: metadata.durationSeconds,
                      }
                    : {},
            name: file.name,
            size: file.size,
            mimetype: file.type || "application/octet-stream",
            kind: validation.kind,
            previewUrl: this.previewUrl(file, validation.kind),
            isVoiceNote: metadata.isVoiceNote === true,
            isRecordedAudio: metadata.isRecordedAudio === true,
            durationSeconds: metadata.durationSeconds || 0,
        }));
        this.local.attachments = [...this.attachments, ...items];
        this.local.uploadError = "";
        for (const item of items) {
            const activeItem = this.attachments.find(
                (candidate) => candidate.id === item.id
            );
            if (activeItem) {
                await this.uploadPendingFile(activeItem, generation, channelId);
            }
        }
        return true;
    }

    async onFileChange(event) {
        const files = Array.from(event.target.files || []);
        event.target.value = "";
        if (files.length) {
            await this.prepareFilesUpload(files);
        }
    }

    async acceptTransferredFile(transfer, event) {
        const files = transferredFiles(transfer);
        if (!files.length) {
            return false;
        }
        if (event && typeof event.preventDefault === "function") {
            event.preventDefault();
        }
        this.local.dragActive = false;
        if (!this.canChooseAttachments) {
            return false;
        }
        return this.prepareFilesUpload(files);
    }

    onPaste(event) {
        if (!transferCarriesFiles(event && event.clipboardData)) {
            return;
        }
        this.acceptTransferredFile(event.clipboardData, event);
    }

    onDragOver(event) {
        if (!transferCarriesFiles(event && event.dataTransfer)) {
            return;
        }
        event.preventDefault();
        this.local.dragActive = true;
    }

    onDragLeave(event) {
        if (
            !event.currentTarget ||
            !event.relatedTarget ||
            !event.currentTarget.contains(event.relatedTarget)
        ) {
            this.local.dragActive = false;
        }
    }

    onDrop(event) {
        this.local.dragActive = false;
        this.acceptTransferredFile(event && event.dataTransfer, event);
    }

    async onVoiceRecordingComplete({file, durationSeconds, isVoiceNote}) {
        this.local.recordingError = "";
        await this.prepareFileUpload(file, {
            isVoiceNote,
            isRecordedAudio: true,
            durationSeconds,
        });
    }

    startVoiceRecording() {
        if (!this.canStartVoiceRecording) {
            return;
        }
        this.local.recordingError = "";
        this.voiceRecorder.start(this.voiceRecordingPolicy);
    }

    stopVoiceRecording() {
        this.voiceRecorder.stop();
    }

    cancelVoiceRecording() {
        this.voiceRecorder.cancel();
    }

    dismissVoiceRecordingError() {
        this.local.recordingError = "";
    }
}

MessageComposer.props = {
    conversation: Object,
    state: Object,
    store: Object,
    draftStorage: {type: Object, optional: true},
    draftDatabase: {type: String, optional: true},
};
MessageComposer.template = "contact_center_ui.MessageComposer";
