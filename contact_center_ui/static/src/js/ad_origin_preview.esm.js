/** @odoo-module **/

import {
    AttributionTouchpoints,
    attributionOccurredAt,
} from "./attribution_touchpoints.esm";
import {Component, useState} from "@odoo/owl";
import {DeferredImage} from "./deferred_image.esm";
import {MessageContent} from "./message_content.esm";
import {normalizeAdOriginPreview} from "./contact_center_model.esm";
import {patch} from "@web/core/utils/patch";

const COLLAPSED_BODY_LENGTH = 240;
const MAX_MESSAGE_PREVIEWS = 8;

export function adOriginPreviewsForMessage(message) {
    if (
        !message ||
        (message.is_deleted && message.deleted_content_visible !== true) ||
        !Array.isArray(message.ad_origin_previews)
    ) {
        return [];
    }
    const seen = new Set();
    const result = [];
    for (const value of message.ad_origin_previews.slice(0, MAX_MESSAGE_PREVIEWS)) {
        const preview = normalizeAdOriginPreview(value);
        if (!preview || seen.has(preview.public_ref)) {
            continue;
        }
        seen.add(preview.public_ref);
        result.push(preview);
    }
    return result;
}

export class AdOriginPreview extends Component {
    setup() {
        this.state = useState({expandedRef: "", failedImageKey: ""});
    }

    get preview() {
        return normalizeAdOriginPreview(this.props.preview);
    }

    get expanded() {
        return this.state.expandedRef === this.preview.public_ref;
    }

    get canExpand() {
        return this.preview.body.length > COLLAPSED_BODY_LENGTH;
    }

    get displayedBody() {
        return this.canExpand && !this.expanded
            ? `${this.preview.body.slice(0, COLLAPSED_BODY_LENGTH).trimEnd()}…`
            : this.preview.body;
    }

    get imageKey() {
        const preview = this.preview;
        return `${preview.public_ref}:${preview.thumbnail_url}:${preview.fetched_at}`;
    }

    get showImage() {
        return Boolean(
            this.preview.thumbnail_url && this.state.failedImageKey !== this.imageKey
        );
    }

    get sourceLabel() {
        return new URL(this.preview.source_url).hostname.endsWith("instagram.com")
            ? "Ver anúncio no Instagram"
            : "Ver anúncio no Facebook";
    }

    get imageErrorHandler() {
        const key = this.imageKey;
        return () => this.onImageError(key);
    }

    get fetchedAt() {
        return attributionOccurredAt(this.preview.fetched_at);
    }

    onImageError(key) {
        if (this.preview && this.imageKey === key) {
            this.state.failedImageKey = key;
        }
    }

    toggleExpanded(event) {
        event.stopPropagation();
        this.state.expandedRef = this.expanded ? "" : this.preview.public_ref;
    }

    stopPropagation(event) {
        event.stopPropagation();
    }
}

AdOriginPreview.template = "contact_center_ui.AdOriginPreview";
AdOriginPreview.props = {preview: Object};
AdOriginPreview.components = {DeferredImage};

export class MessageAdOriginPreviews extends Component {
    get previews() {
        return adOriginPreviewsForMessage(this.props.message);
    }
}

MessageAdOriginPreviews.template = "contact_center_ui.MessageAdOriginPreviews";
MessageAdOriginPreviews.props = {message: Object};
MessageAdOriginPreviews.components = {AdOriginPreview};

patch(MessageContent, "contact_center_ui.ad_origin_preview_components", {
    components: {...MessageContent.components, MessageAdOriginPreviews},
});
patch(AttributionTouchpoints, "contact_center_ui.ad_origin_preview_components", {
    components: {...AttributionTouchpoints.components, AdOriginPreview},
});
