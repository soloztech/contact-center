/** @odoo-module **/

import {Component, useState} from "@odoo/owl";
import {MessageContent} from "@contact_center_ui/js/message_content.esm";
import {patch} from "@web/core/utils/patch";
import {useService} from "@web/core/utils/hooks";

const TRANSCRIPTION_STATES = new Set(["idle", "pending", "done", "failed", "skipped"]);
const COLLAPSED_TEXT_LENGTH = 420;

export function normalizeTranscription(value, mediaId) {
    if (
        !value ||
        typeof value !== "object" ||
        !Number.isSafeInteger(mediaId) ||
        mediaId <= 0 ||
        value.media_id !== mediaId ||
        !TRANSCRIPTION_STATES.has(value.state)
    ) {
        return null;
    }
    return {
        media_id: mediaId,
        state: value.state,
        text:
            value.state === "done" && typeof value.text === "string" ? value.text : "",
        can_request:
            value.can_request === true && !["pending", "done"].includes(value.state),
    };
}

export function transcriptionForMedia(message, media) {
    if (
        !message ||
        !media ||
        media.kind !== "audio" ||
        media.state !== "ready" ||
        (message.is_deleted && message.deleted_content_visible !== true) ||
        !Array.isArray(message.transcriptions)
    ) {
        return null;
    }
    const descriptor = message.transcriptions.find(
        (item) => item && item.media_id === media.id
    );
    const result = normalizeTranscription(descriptor, media.id);
    if (result && message.is_deleted) {
        result.can_request = false;
    }
    return result;
}

export class AudioTranscription extends Component {
    setup() {
        this.orm = useService("orm");
        this.state = useState({
            requesting: false,
            requestFailed: false,
            response: null,
            responseBaseline: "",
            expanded: false,
            copied: false,
            copyFailed: false,
        });
    }

    get serverDescriptor() {
        return transcriptionForMedia(this.props.message, this.props.media);
    }

    get descriptor() {
        const descriptor = this.serverDescriptor;
        if (!descriptor) {
            return null;
        }
        // An immediate RPC response covers the bus round trip, while a fresh
        // timeline projection always wins over that local response.
        if (
            this.state.response &&
            this.state.responseBaseline === JSON.stringify(descriptor)
        ) {
            return this.state.response;
        }
        return descriptor;
    }

    get visible() {
        const descriptor = this.descriptor;
        return Boolean(
            descriptor && (descriptor.can_request || descriptor.state !== "idle")
        );
    }

    get pending() {
        return (
            this.descriptor.state !== "done" &&
            (this.state.requesting || this.descriptor.state === "pending")
        );
    }

    get failed() {
        return this.state.requestFailed || this.descriptor.state === "failed";
    }

    get canExpand() {
        return this.descriptor.text.length > COLLAPSED_TEXT_LENGTH;
    }

    get displayedText() {
        const text = this.descriptor.text;
        return this.canExpand && !this.state.expanded
            ? `${text.slice(0, COLLAPSED_TEXT_LENGTH).trimEnd()}…`
            : text;
    }

    toggleExpanded(event) {
        event.stopPropagation();
        this.state.expanded = !this.state.expanded;
    }

    async request(event) {
        if (event) {
            event.stopPropagation();
        }
        const descriptor = this.descriptor;
        if (!descriptor || !descriptor.can_request || this.state.requesting) {
            return;
        }
        const baseline = JSON.stringify(this.serverDescriptor);
        this.state.requesting = true;
        this.state.requestFailed = false;
        try {
            const response = await this.orm.call(
                "contact.center.ui.api",
                "request_transcription",
                [this.props.media.id]
            );
            const result = normalizeTranscription(response, this.props.media.id);
            if (!result) {
                this.state.requestFailed = true;
                return;
            }
            this.state.responseBaseline = baseline;
            this.state.response = result;
        } catch (_error) {
            // Never put RPC or provider details into the conversation bubble.
            this.state.requestFailed = true;
        } finally {
            this.state.requesting = false;
        }
    }

    async copy(event) {
        event.stopPropagation();
        const descriptor = this.descriptor;
        if (!descriptor || descriptor.state !== "done" || !descriptor.text) {
            return;
        }
        this.state.copied = false;
        this.state.copyFailed = false;
        try {
            await navigator.clipboard.writeText(descriptor.text);
            this.state.copied = true;
        } catch (_error) {
            this.state.copyFailed = true;
            this.state.expanded = true;
        }
    }
}

AudioTranscription.template = "contact_center_ui.AudioTranscription";
AudioTranscription.props = {message: Object, media: Object};

patch(MessageContent, "contact_center_ui.transcription_components", {
    components: {...MessageContent.components, AudioTranscription},
});
