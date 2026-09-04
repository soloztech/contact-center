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
import {deserializeDateTime} from "@web/core/l10n/dates";
import {session} from "@web/session";

const {DateTime} = luxon;

const DRAFT_STORAGE_VERSION = 2;
const DRAFT_STORAGE_PREFIX = "contact_center_ui.composer_drafts";
const COMPOSER_MODES = new Set(["message", "note"]);
const PRODUCTIVITY_TOOLS = new Set(["quick_reply", "followup", "schedule"]);
const MAX_PERSISTED_DRAFTS = 100;
const MAX_DRAFT_BODY_LENGTH = 65536;
const MAX_RETAINED_UPLOAD_PREVIEW_BYTES = 8 * 1024 * 1024;
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
        followupCaseId: draft.followupCaseId || false,
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
            followupCaseId: false,
            completionFeedback: {},
            attachment: false,
            mediaRef: "",
            uploadPhase: "idle",
            uploadError: "",
            recordingPhase: "idle",
            recordingElapsedMs: 0,
            recordingError: "",
            dragActive: false,
        });
        this.uploadGeneration = 0;
        this.uploadController = null;
        this.pendingFile = null;
        this.pendingUploadId = "";
        this.pendingUploadMetadata = {};
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
            this.store.registerConversationSelectionGuard((channelId) =>
                this.prepareConversationSwitch(channelId)
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
                    this.removeAttachment();
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
            this.saveCurrentDraft();
            window.removeEventListener("keydown", this.onWindowKeydown);
            if (this.unregisterConversationGuard) {
                this.unregisterConversationGuard();
            }
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
                cases: [],
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
            !this.local.attachment,
            !this.local.mediaRef,
            !this.state.replyTo,
            !this.voiceCaptureActive,
            this.local.uploadPhase !== "uploading",
            this.local.uploadPhase !== "error",
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
            this.conversationPolicy.allow_attachments
        );
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
            this.voiceCaptureActive ||
                this.local.uploadPhase === "uploading" ||
                this.local.actionBusy
        );
    }

    prepareConversationSwitch(channelId) {
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
                    draft.attachment ||
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
            attachment: this.local.attachment ? {...this.local.attachment} : false,
            mediaRef: this.local.mediaRef,
            uploadPhase: this.local.uploadPhase,
            uploadError: this.local.uploadError,
            pendingFile: this.pendingFile,
            pendingUploadId: this.pendingUploadId,
            pendingUploadMetadata: {...this.pendingUploadMetadata},
            replyTo: this.state.replyTo ? {...this.state.replyTo} : false,
            scheduledAt: this.local.scheduledAt,
            followupSummary: this.local.followupSummary,
            followupNote: this.local.followupNote,
            followupDate: this.local.followupDate,
            followupActivityTypeId: this.local.followupActivityTypeId,
            followupUserId: this.local.followupUserId,
            followupCaseId: this.local.followupCaseId,
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

    clearLocalAttachment({revoke = true} = {}) {
        this.uploadGeneration += 1;
        if (this.uploadController) {
            this.uploadController.abort();
            this.uploadController = null;
        }
        if (revoke) {
            this.revokePreview();
        }
        this.local.attachment = false;
        this.local.mediaRef = "";
        this.local.uploadPhase = "idle";
        this.local.uploadError = "";
        this.pendingFile = null;
        this.pendingUploadId = "";
        this.pendingUploadMetadata = {};
        if (this.fileRef.el) {
            this.fileRef.el.value = "";
        }
    }

    resetLocalForConversation({revoke = true} = {}) {
        this.cancelVoiceRecording();
        this.clearLocalAttachment({revoke});
        this.local.body = "";
        this.local.mode = "message";
        this.local.tool = false;
        this.local.scheduledAt = "";
        this.local.followupSummary = "";
        this.local.followupNote = "";
        this.local.followupDate = "";
        this.local.followupActivityTypeId = false;
        this.local.followupUserId = false;
        this.local.followupCaseId = false;
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
        this.local.attachment = draft.attachment || false;
        this.local.mediaRef = draft.mediaRef || "";
        this.local.uploadPhase = draft.uploadPhase || "idle";
        this.local.uploadError = draft.uploadError || "";
        this.pendingFile = draft.pendingFile || null;
        this.pendingUploadId = draft.pendingUploadId || "";
        this.pendingUploadMetadata = draft.pendingUploadMetadata || {};
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
                if (this.local.uploadPhase === "uploading") {
                    this.local.uploadPhase = "error";
                    this.local.uploadError =
                        "O upload foi interrompido pela troca de conversa. Tente novamente.";
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

    revokeDraftPreview(draft, revoked) {
        const previewUrl = draft && draft.attachment && draft.attachment.previewUrl;
        if (
            previewUrl &&
            !revoked.has(previewUrl) &&
            window.URL &&
            typeof window.URL.revokeObjectURL === "function"
        ) {
            revoked.add(previewUrl);
            window.URL.revokeObjectURL(previewUrl);
        }
    }

    disposeDrafts() {
        const revoked = new Set();
        this.revokeDraftPreview({attachment: this.local.attachment}, revoked);
        for (const draft of this.drafts.values()) {
            this.revokeDraftPreview(draft, revoked);
        }
        this.drafts.clear();
        this.clearLocalAttachment({revoke: false});
    }

    get canStartVoiceRecording() {
        return Boolean(
            this.voiceRecordingAvailable &&
                this.conversationPolicy.allow_send &&
                this.canAttach &&
                !this.local.body.trim() &&
                !this.local.attachment &&
                this.local.uploadPhase === "idle" &&
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
                !this.local.body.trim() &&
                !this.local.attachment &&
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
            !this.local.attachment,
            !this.state.replyTo,
            !this.voiceCaptureActive,
            !this.local.actionBusy,
        ].every(Boolean);
    }

    get canSendExternalMessage() {
        const captionAllowed =
            !this.local.attachment ||
            !this.local.body.trim() ||
            mediaCaptionAllowed(this.capabilities, this.local.attachment.kind);
        return [
            this.conversationPolicy.allow_send,
            Boolean(this.local.body.trim() || this.local.mediaRef),
            !this.local.mediaRef || this.canAttach,
            !this.state.replyTo || this.conversationPolicy.allow_reply,
            captionAllowed,
            this.local.uploadPhase !== "error",
            this.local.uploadPhase !== "uploading",
            !this.voiceCaptureActive,
            !this.sending,
            !this.local.actionBusy,
        ].every(Boolean);
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
        if (this.local.uploadPhase === "uploading") {
            return "Aguarde o envio do arquivo";
        }
        if (this.local.uploadPhase === "error") {
            return "Remova o arquivo com erro para continuar";
        }
        if (this.voiceCaptureActive) {
            return "Conclua ou cancele a gravação de áudio";
        }
        if (
            this.local.attachment &&
            this.local.body.trim() &&
            !mediaCaptionAllowed(this.capabilities, this.local.attachment.kind)
        ) {
            return "Este tipo de arquivo não aceita legenda neste provedor";
        }
        return "Escreva uma mensagem";
    }

    get attachmentLabel() {
        const attachment = this.local.attachment;
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
            if (this.voiceCaptureActive || this.local.uploadPhase === "uploading") {
                this.store.notify(
                    "Conclua ou cancele o áudio ou arquivo antes de criar uma nota.",
                    {type: "warning", title: "Trabalho em andamento"}
                );
                return false;
            }
            if (this.local.attachment) {
                this.store.notify("Remova o anexo antes de criar uma nota interna.", {
                    type: "warning",
                    title: "Nota somente em texto",
                });
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
        if (!PRODUCTIVITY_TOOLS.has(tool) || this.local.actionBusy) {
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
                this.local.attachment ||
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

    onQuickReplySearch(event) {
        this.local.quickReplyQuery = event.target.value;
        this.store.searchQuickReplies(this.local.quickReplyQuery);
    }

    insertQuickReply(reply) {
        if (!reply || typeof reply.body !== "string") {
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
        this.local[name] = [
            "followupActivityTypeId",
            "followupUserId",
            "followupCaseId",
        ].includes(name)
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
        if (this.local.followupCaseId) {
            values.case_id = this.local.followupCaseId;
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
        const body = this.local.body;
        const channelId = this.conversation.channel_id;
        const mediaRef = this.local.mediaRef;
        this.local.actionBusy = true;
        let sent = false;
        try {
            sent = this.noteMode
                ? await this.store.postInternalNote(body)
                : await this.store.sendMessage(body, mediaRef ? [mediaRef] : []);
        } finally {
            this.local.actionBusy = false;
        }
        if (sent) {
            this.clearSentDraft(channelId, body, mediaRef);
            this.clearPersistedDraft(channelId);
        }
        if (sent && this.conversation && this.conversation.channel_id === channelId) {
            const bodyWasNotChanged = this.local.body === body;
            const sentMediaIsStillSelected =
                mediaRef && this.local.mediaRef === mediaRef;
            if (bodyWasNotChanged) {
                this.local.body = "";
            }
            if (sentMediaIsStillSelected) {
                this.removeAttachment();
            }
            this.resize();
            this.persistDraft(channelId);
            if (this.inputRef.el) {
                this.inputRef.el.focus();
            }
        }
    }

    clearSentDraft(channelId, body, mediaRef) {
        if (this.conversation && this.conversation.channel_id === channelId) {
            return false;
        }
        const draft = this.drafts.get(channelId);
        if (!draft) {
            return false;
        }
        if (draft.body === body) {
            draft.body = "";
        }
        if (mediaRef && draft.mediaRef === mediaRef) {
            this.revokeDraftPreview(draft, new Set());
            draft.attachment = false;
            draft.mediaRef = "";
            draft.uploadPhase = "idle";
            draft.uploadError = "";
            draft.pendingFile = null;
            draft.pendingUploadId = "";
            draft.pendingUploadMetadata = {};
        }
        draft.replyTo = false;
        if (!this.draftHasContent(draft)) {
            this.drafts.delete(channelId);
        }
        this.clearPersistedDraft(channelId);
        return true;
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
        if (
            this.canAttach &&
            !this.voiceCaptureActive &&
            !this.sending &&
            this.fileRef.el
        ) {
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

    revokePreview() {
        const previewUrl = this.local.attachment && this.local.attachment.previewUrl;
        if (
            previewUrl &&
            window.URL &&
            typeof window.URL.revokeObjectURL === "function"
        ) {
            window.URL.revokeObjectURL(previewUrl);
        }
    }

    removeAttachment() {
        this.clearLocalAttachment({revoke: true});
    }

    uploadIsStale(generation, channelId = false) {
        return (
            generation !== this.uploadGeneration ||
            (channelId && channelId !== this.state.selectedChannelId)
        );
    }

    startUploadController() {
        const controller =
            typeof window.AbortController === "function"
                ? new window.AbortController()
                : null;
        this.uploadController = controller;
        return controller;
    }

    applyUploadedMedia(payload) {
        const serverMedia = normalizeMessageMedia([payload.media])[0];
        const uploadedSize = uploadedMediaSize(
            serverMedia,
            this.pendingFile,
            this.local.attachment
        );
        const releasePreview = Boolean(
            this.local.attachment &&
                this.local.attachment.previewUrl &&
                uploadedSize > MAX_RETAINED_UPLOAD_PREVIEW_BYTES
        );
        if (releasePreview) {
            this.revokePreview();
        }
        this.local.attachment = mergedUploadedAttachment(
            serverMedia,
            this.local.attachment,
            releasePreview
        );
        this.local.mediaRef = payload.media_ref.trim();
        this.local.uploadPhase = "ready";
        // The immutable server reference owns the prepared upload from here on.
        // Keeping the browser File in each conversation draft would retain the
        // complete binary in memory until that draft is sent or discarded.
        this.pendingFile = null;
        this.pendingUploadId = "";
        this.pendingUploadMetadata = {};
    }

    async uploadPendingFile(generation, channelId) {
        const file = this.pendingFile;
        const uploadId = this.pendingUploadId;
        const uploadMetadata = this.pendingUploadMetadata;
        const controller = this.startUploadController();
        try {
            const payload = await this.store.uploadMedia(
                file,
                uploadId,
                channelId,
                controller ? controller.signal : undefined,
                uploadMetadata
            );
            if (this.uploadIsStale(generation, channelId)) {
                return;
            }
            this.applyUploadedMedia(payload);
        } catch (error) {
            if (
                this.uploadIsStale(generation) ||
                (error && error.name === "AbortError")
            ) {
                return;
            }
            this.local.mediaRef = "";
            this.local.uploadPhase = "error";
            this.local.uploadError =
                (error && error.message) || "O arquivo não pôde ser enviado.";
        } finally {
            if (this.uploadController === controller) {
                this.uploadController = null;
            }
        }
    }

    retryUpload() {
        if (
            !this.pendingFile ||
            !this.pendingUploadId ||
            this.local.uploadPhase !== "error"
        ) {
            return;
        }
        const generation = ++this.uploadGeneration;
        this.local.uploadPhase = "uploading";
        this.local.uploadError = "";
        this.uploadPendingFile(generation, this.conversation.channel_id);
    }

    async prepareFileUpload(file, metadata = {}) {
        const validation = validateMediaFile(file, this.capabilities);
        if (!validation.ok) {
            this.removeAttachment();
            this.local.uploadPhase = "error";
            this.local.uploadError = validation.error;
            return;
        }
        if (!this.conversation) {
            return;
        }
        this.removeAttachment();
        const generation = ++this.uploadGeneration;
        const channelId = this.conversation.channel_id;
        this.pendingFile = file;
        this.pendingUploadId = makeClientRequestId(window.crypto);
        this.pendingUploadMetadata =
            Number.isSafeInteger(metadata.durationSeconds) &&
            metadata.durationSeconds > 0
                ? {
                      isVoiceNote: metadata.isVoiceNote === true,
                      durationSeconds: metadata.durationSeconds,
                  }
                : {};
        this.local.attachment = {
            name: file.name,
            size: file.size,
            mimetype: file.type || "application/octet-stream",
            kind: validation.kind,
            previewUrl: this.previewUrl(file, validation.kind),
            isVoiceNote: metadata.isVoiceNote === true,
            isRecordedAudio: metadata.isRecordedAudio === true,
            durationSeconds: metadata.durationSeconds || 0,
        };
        this.local.uploadPhase = "uploading";
        this.local.uploadError = "";
        await this.uploadPendingFile(generation, channelId);
    }

    async onFileChange(event) {
        const file = event.target.files && event.target.files[0];
        if (file) {
            await this.prepareFileUpload(file);
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
        if (!this.canAttach || this.voiceCaptureActive || this.sending) {
            return false;
        }
        if (this.local.attachment) {
            this.store.notify("Remova o anexo atual antes de adicionar outro.", {
                type: "warning",
                title: "Um arquivo por mensagem",
            });
            return false;
        }
        if (files.length > 1) {
            this.store.notify("Selecione apenas um arquivo por mensagem.", {
                type: "warning",
                title: "Vários arquivos detectados",
            });
            return false;
        }
        await this.prepareFileUpload(files[0]);
        return true;
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
