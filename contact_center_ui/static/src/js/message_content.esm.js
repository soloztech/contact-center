/** @odoo-module **/

import {
    Component,
    onMounted,
    onWillUnmount,
    useEffect,
    useRef,
    useState,
} from "@odoo/owl";
import {useChildRef, useOwnedDialogs} from "@web/core/utils/hooks";
import {DeferredImage} from "./deferred_image.esm";
import {Dialog} from "@web/core/dialog/dialog";
import {MessageLinkPreviews} from "./link_preview.esm";
import {formatFileSize} from "./contact_center_model.esm";
import {structuredMessageCard} from "./structured_content.esm";

const MEDIA_LABELS = Object.freeze({
    image: "Imagem",
    audio: "Áudio",
    video: "Vídeo",
    document: "Documento",
});
const VIEWABLE_MEDIA_KINDS = new Set(["image", "video"]);
const PDF_MIMETYPE = "application/pdf";
const PDFJS_VIEWER_URL = "/web/static/lib/pdfjs/web/viewer.html?file=";
const AUDIO_SPEEDS = Object.freeze([1, 1.5, 2]);
const AUDIO_WAVEFORM = Object.freeze([
    32, 58, 44, 76, 52, 88, 63, 39, 70, 96, 61, 48, 82, 57, 73, 42, 66, 91, 54, 36, 79,
    62, 46, 71,
]);
const LOCAL_MEDIA_URL =
    /^\/contact_center\/media\/([1-9]\d*)\/content(?:\?download=1)?$/;
const LOCAL_MEDIA_CONTENT_URL = /^\/contact_center\/media\/([1-9]\d*)\/content$/;
const CONTROL_TIMELINE_MESSAGE_META = Object.freeze({
    "call.offer": Object.freeze({
        family: "call",
        icon: "fa fa-phone",
        label: "Evento de chamada",
        fallback: "Chamada recebida",
    }),
    "call.accept": Object.freeze({
        family: "call",
        icon: "fa fa-phone",
        label: "Evento de chamada",
        fallback: "Chamada atendida",
    }),
    "call.terminate": Object.freeze({
        family: "call",
        icon: "fa fa-phone",
        label: "Evento de chamada",
        fallback: "Chamada encerrada",
    }),
    "identity.security.changed": Object.freeze({
        family: "identity",
        icon: "fa fa-shield",
        label: "Segurança da conversa",
        fallback: "A segurança desta conversa foi atualizada",
    }),
});

let activeAudioElement = null;
let activeVideoElement = null;

/**
 * Return the fixed presentation metadata for a provider-neutral control event.
 * Provider values and inherited properties are deliberately ignored.
 *
 * @param {Object} message timeline message DTO
 * @returns {Object|false} immutable control presentation metadata
 */
export function controlTimelineMessageMeta(message) {
    if (
        !message ||
        typeof message !== "object" ||
        !Object.prototype.hasOwnProperty.call(message, "content_type") ||
        typeof message.content_type !== "string"
    ) {
        return false;
    }
    if (
        !Object.prototype.hasOwnProperty.call(
            CONTROL_TIMELINE_MESSAGE_META,
            message.content_type
        )
    ) {
        return false;
    }
    return CONTROL_TIMELINE_MESSAGE_META[message.content_type];
}

export function formatAudioTime(value) {
    const seconds = typeof value === "number" ? value : NaN;
    if (!Number.isFinite(seconds) || seconds < 0) {
        return "0:00";
    }
    const total = Math.floor(seconds);
    const minutes = Math.floor(total / 60);
    return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

export function isLocalMediaUrl(value) {
    return typeof value === "string" && LOCAL_MEDIA_URL.test(value);
}

function localMediaId(value) {
    const match = typeof value === "string" && value.match(LOCAL_MEDIA_URL);
    return match ? Number(match[1]) : false;
}

function localMediaContentId(value) {
    const match = typeof value === "string" && value.match(LOCAL_MEDIA_CONTENT_URL);
    return match ? Number(match[1]) : false;
}

function normalizedMimetype(value) {
    if (typeof value !== "string") {
        return "";
    }
    return value.split(";", 1)[0].trim().toLowerCase();
}

export function isViewablePdf(media) {
    return Boolean(
        media &&
            media.state === "ready" &&
            media.kind === "document" &&
            Number.isSafeInteger(media.id) &&
            media.id > 0 &&
            normalizedMimetype(media.mimetype) === PDF_MIMETYPE &&
            localMediaContentId(media.content_url) === media.id
    );
}

export function pdfJsViewerUrl(contentUrl) {
    if (localMediaContentId(contentUrl) === false) {
        return "";
    }
    return `${PDFJS_VIEWER_URL}${encodeURIComponent(contentUrl)}#pagemode=none`;
}

function sanitizedViewerItem(media) {
    const isPdf = isViewablePdf(media);
    if (
        !media ||
        media.state !== "ready" ||
        (!VIEWABLE_MEDIA_KINDS.has(media.kind) && !isPdf) ||
        !Number.isSafeInteger(media.id) ||
        media.id <= 0 ||
        localMediaId(media.content_url) !== media.id
    ) {
        return null;
    }
    return {
        id: media.id,
        kind: media.kind,
        state: "ready",
        name:
            typeof media.name === "string" && media.name.trim()
                ? media.name.trim().slice(0, 240)
                : MEDIA_LABELS[media.kind],
        mimetype: isPdf
            ? PDF_MIMETYPE
            : typeof media.mimetype === "string"
            ? media.mimetype.trim().slice(0, 160)
            : "",
        content_url: media.content_url,
        download_url:
            localMediaId(media.download_url) === media.id
                ? media.download_url
                : media.content_url,
        ...(isPdf ? {pdf_viewer_url: pdfJsViewerUrl(media.content_url)} : {}),
    };
}

export function viewableMediaItems(mediaItems) {
    if (!Array.isArray(mediaItems)) {
        return [];
    }
    return mediaItems.map(sanitizedViewerItem).filter(Boolean);
}

export function downloadableMessageMedia(message) {
    if (
        !message ||
        (message.is_deleted && message.deleted_content_visible !== true) ||
        controlTimelineMessageMeta(message) ||
        !Array.isArray(message.media)
    ) {
        return [];
    }
    const seen = new Set();
    return message.media.flatMap((media) => {
        if (
            !media ||
            media.state !== "ready" ||
            !Object.prototype.hasOwnProperty.call(MEDIA_LABELS, media.kind) ||
            !Number.isSafeInteger(media.id) ||
            media.id <= 0 ||
            localMediaContentId(media.content_url) !== media.id ||
            seen.has(media.id)
        ) {
            return [];
        }
        seen.add(media.id);
        return [
            {
                id: media.id,
                label: `Baixar ${MEDIA_LABELS[media.kind].toLocaleLowerCase()}`,
                name:
                    typeof media.name === "string" && media.name.trim()
                        ? media.name.trim().slice(0, 240)
                        : MEDIA_LABELS[media.kind],
                download_url:
                    localMediaId(media.download_url) === media.id
                        ? media.download_url
                        : `${media.content_url}?download=1`,
            },
        ];
    });
}

export function hasCompactAudio(message) {
    const media = message && message.media;
    return Boolean(
        message &&
            !message.is_deleted &&
            !(message.body_text || "").trim() &&
            !structuredMessageCard(message.structured_content) &&
            Array.isArray(media) &&
            media.length === 1 &&
            media[0] &&
            media[0].kind === "audio" &&
            media[0].state === "ready" &&
            localMediaContentId(media[0].content_url) === media[0].id
    );
}

export class MediaViewer extends Component {
    setup() {
        const startIndex = Number.isInteger(this.props.startIndex)
            ? this.props.startIndex
            : 0;
        this.state = useState({
            index: Math.min(Math.max(startIndex, 0), this.props.items.length - 1),
        });
        this.modalRef = useChildRef();
        this.viewerRef = useRef("viewer");
        this.videoRef = useRef("video");
        useEffect(
            () => {
                // Keep the actual node: dialog teardown can clear Owl refs first.
                this.videoElement = this.videoRef.el;
                return () => this.stopVideo();
            },
            () => [this.current.id]
        );
        onMounted(() => {
            if (this.modalRef.el) {
                this.modalRef.el.setAttribute("aria-label", "Visualizador de mídia");
            }
            if (this.viewerRef.el) {
                this.viewerRef.el.focus();
            }
        });
    }

    get current() {
        return this.props.items[this.state.index];
    }

    get positionLabel() {
        return `${this.state.index + 1} de ${this.props.items.length}`;
    }

    get viewerAriaLabel() {
        const content =
            this.current.kind === "document"
                ? `Documento PDF ${this.current.name}`
                : `Conteúdo da mídia ${this.current.name}`;
        return this.props.items.length > 1
            ? `${content}; use as setas para navegar`
            : content;
    }

    stopVideo() {
        const video = this.videoElement;
        this.videoElement = null;
        if (video) {
            video.pause();
            if (document.pictureInPictureElement === video) {
                document.exitPictureInPicture().catch(() => false);
            }
            if (activeVideoElement === video) {
                activeVideoElement = null;
            }
        }
    }

    onVideoPlay(event) {
        const video = event.currentTarget;
        if (activeVideoElement && activeVideoElement !== video) {
            activeVideoElement.pause();
        }
        if (activeAudioElement) {
            activeAudioElement.pause();
        }
        activeVideoElement = video;
    }

    previous() {
        if (this.state.index > 0) {
            this.state.index -= 1;
        }
    }

    next() {
        if (this.state.index < this.props.items.length - 1) {
            this.state.index += 1;
        }
    }

    onKeydown(event) {
        const target = event.target;
        if (
            !target ||
            ["INPUT", "SELECT", "TEXTAREA", "VIDEO"].includes(target.tagName) ||
            target.isContentEditable
        ) {
            return;
        }
        if (event.key === "ArrowLeft" && this.state.index > 0) {
            event.preventDefault();
            this.previous();
        } else if (
            event.key === "ArrowRight" &&
            this.state.index < this.props.items.length - 1
        ) {
            event.preventDefault();
            this.next();
        }
    }
}

MediaViewer.components = {Dialog};
MediaViewer.props = {
    close: Function,
    items: Array,
    startIndex: Number,
};
MediaViewer.template = "contact_center_ui.MediaViewer";

export class AudioPlayer extends Component {
    setup() {
        this.audioRef = useRef("audio");
        this.state = useState({
            currentTime: 0,
            duration: this.initialDuration,
            error: false,
            playing: false,
            rate: 1,
        });
        onWillUnmount(() => {
            const audio = this.audio;
            if (audio) {
                audio.pause();
            }
            if (activeAudioElement === audio) {
                activeAudioElement = null;
            }
        });
    }

    get audio() {
        return this.audioRef.el;
    }

    get media() {
        return this.props.media;
    }

    get initialDuration() {
        const duration = Number(this.props.media.duration_seconds);
        return Number.isFinite(duration) && duration >= 0 ? duration : 0;
    }

    get label() {
        return this.media.is_voice_note
            ? "Mensagem de voz"
            : this.media.name || "Áudio";
    }

    get timeLabel() {
        return formatAudioTime(
            this.state.playing || this.state.currentTime > 0
                ? this.state.currentTime
                : this.state.duration
        );
    }

    get positionLabel() {
        return `${formatAudioTime(this.state.currentTime)} de ${formatAudioTime(
            this.state.duration
        )}`;
    }

    get waveform() {
        return AUDIO_WAVEFORM;
    }

    get progressRatio() {
        if (!this.state.duration) {
            return 0;
        }
        return Math.min(1, Math.max(0, this.state.currentTime / this.state.duration));
    }

    barClass(index) {
        return (index + 1) / this.waveform.length <= this.progressRatio
            ? "cc-audio-wave__bar is-played"
            : "cc-audio-wave__bar";
    }

    async togglePlayback() {
        if (!this.audio || this.state.error) {
            return;
        }
        if (this.audio.paused) {
            try {
                await this.audio.play();
            } catch (_error) {
                this.state.error = true;
                this.state.playing = false;
            }
        } else {
            this.audio.pause();
        }
    }

    onLoadedMetadata() {
        if (this.audio && Number.isFinite(this.audio.duration)) {
            this.state.duration = this.audio.duration;
        }
        this.state.error = false;
    }

    onTimeUpdate() {
        if (this.audio) {
            this.state.currentTime = this.audio.currentTime || 0;
        }
    }

    onPlay() {
        if (activeAudioElement && activeAudioElement !== this.audio) {
            activeAudioElement.pause();
        }
        if (activeVideoElement) {
            activeVideoElement.pause();
        }
        activeAudioElement = this.audio;
        this.state.playing = true;
        this.state.error = false;
    }

    onPause() {
        this.state.playing = false;
    }

    onEnded() {
        this.state.playing = false;
        this.state.currentTime = 0;
        if (activeAudioElement === this.audio) {
            activeAudioElement = null;
        }
    }

    onError() {
        this.state.error = true;
        this.state.playing = false;
        if (activeAudioElement === this.audio) {
            activeAudioElement = null;
        }
    }

    onSeek(event) {
        if (!this.audio || !this.state.duration) {
            return;
        }
        const target = Number(event.target.value);
        if (!Number.isFinite(target)) {
            return;
        }
        const nextTime = Math.min(Math.max(target, 0), this.state.duration);
        this.audio.currentTime = nextTime;
        this.state.currentTime = nextTime;
    }

    cycleRate() {
        const index = AUDIO_SPEEDS.indexOf(this.state.rate);
        const rate = AUDIO_SPEEDS[(index + 1) % AUDIO_SPEEDS.length];
        this.state.rate = rate;
        if (this.audio) {
            this.audio.playbackRate = rate;
        }
    }
}

AudioPlayer.props = {media: Object, slots: {type: Object, optional: true}};
AudioPlayer.template = "contact_center_ui.AudioPlayer";

export class MessageContent extends Component {
    setup() {
        this.addDialog = useOwnedDialogs();
        this.imageLoad = useState({attempts: {}, failures: {}});
        this.viewerState = useState({items: []});
        this.closeMediaViewer = null;
        useEffect(
            () => {
                if (this.closeMediaViewer && !this.viewerMediaAvailable) {
                    this.closeMediaViewer();
                }
            },
            () => [this.viewerMediaAvailable]
        );
    }

    get message() {
        return this.props.message;
    }

    get viewerMediaAvailable() {
        if (this.message.is_deleted && this.message.deleted_content_visible !== true) {
            return false;
        }
        const currentItems = viewableMediaItems(this.message.media);
        // Dialogs own a gallery snapshot. Close it when any item is revoked so
        // neither the current view nor gallery navigation can expose stale media.
        return this.viewerState.items.every((selected) =>
            currentItems.some(
                (media) =>
                    media.id === selected.id &&
                    media.kind === selected.kind &&
                    media.content_url === selected.content_url
            )
        );
    }

    get structuredCard() {
        return structuredMessageCard(this.message.structured_content);
    }

    get controlMeta() {
        return controlTimelineMessageMeta(this.message);
    }

    get controlText() {
        const body =
            typeof this.message.body_text === "string"
                ? this.message.body_text.trim().slice(0, 240)
                : "";
        return body || (this.controlMeta && this.controlMeta.fallback) || "";
    }

    mediaLabel(media) {
        if (media.kind === "audio" && media.is_voice_note) {
            return "Mensagem de voz";
        }
        return MEDIA_LABELS[media.kind] || "Arquivo";
    }

    fileLabel(media) {
        return media.name || this.mediaLabel(media);
    }

    fileMeta(media) {
        return [this.mediaLabel(media), formatFileSize(media.size_bytes)]
            .filter(Boolean)
            .join(" · ");
    }

    contentUrl(media) {
        return media.content_url || "";
    }

    isViewablePdf(media) {
        return isViewablePdf(media);
    }

    imageKey(media) {
        const id =
            Number.isSafeInteger(media && media.id) && media.id > 0 ? media.id : 0;
        return id ? `id:${id}` : `url:${this.contentUrl(media)}`;
    }

    imageAttempt(media) {
        return this.imageLoad.attempts[this.imageKey(media)] || 0;
    }

    imageRenderKey(media) {
        return `${this.imageKey(media)}:${this.imageAttempt(media)}`;
    }

    imageFailed(media) {
        const key = this.imageKey(media);
        return this.imageLoad.failures[key] === this.imageAttempt(media) + 1;
    }

    onImageLoadError(media, attempt) {
        const key = this.imageKey(media);
        if (attempt !== this.imageAttempt(media)) {
            return false;
        }
        this.imageLoad.failures[key] = attempt + 1;
        return true;
    }

    retryImage(media, event) {
        if (event) {
            event.preventDefault();
            event.stopPropagation();
        }
        const key = this.imageKey(media);
        const attempt = this.imageAttempt(media);
        if (this.imageLoad.failures[key] !== attempt + 1) {
            return false;
        }
        this.imageLoad.failures[key] = 0;
        this.imageLoad.attempts[key] = attempt + 1;
        return true;
    }

    mediaClass(media) {
        return ["cc-media", `cc-media--${media.kind}`, `is-${media.state}`].join(" ");
    }

    openViewer(media, event) {
        if (this.message.is_deleted && this.message.deleted_content_visible !== true) {
            return;
        }
        const items = viewableMediaItems(this.message.media);
        const startIndex = items.findIndex(
            (item) =>
                item.id === media.id &&
                item.kind === media.kind &&
                item.content_url === media.content_url
        );
        if (startIndex < 0) {
            return;
        }
        const opener = event && event.currentTarget;
        if (this.closeMediaViewer) {
            this.closeMediaViewer();
        }
        this.viewerState.items = items;
        this.closeMediaViewer = this.addDialog(
            MediaViewer,
            {items, startIndex},
            {
                onClose: () => {
                    this.closeMediaViewer = null;
                    this.viewerState.items = [];
                    if (opener && opener.isConnected) {
                        opener.focus();
                    }
                },
            }
        );
    }
}

MessageContent.components = {
    AudioPlayer,
    DeferredImage,
    MessageLinkPreviews,
};
MessageContent.props = {message: Object, slots: {type: Object, optional: true}};
MessageContent.template = "contact_center_ui.MessageContent";
