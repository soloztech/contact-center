/** @odoo-module **/

import {Component, useEffect} from "@odoo/owl";
import {useService} from "@web/core/utils/hooks";

export function safePreviewUrl(value) {
    if (typeof value !== "string" || !/^https?:\/\//i.test(value)) {
        return "";
    }
    try {
        const url = new URL(value);
        return url.username || url.password ? "" : url.href;
    } catch (_error) {
        return "";
    }
}

export class MessageLinkPreviews extends Component {
    setup() {
        this.orm = useService("orm");
        useEffect(
            () => {
                const message = this.props.message;
                if (
                    Number.isSafeInteger(message.channel_id) &&
                    Number.isSafeInteger(message.message_id) &&
                    !message.is_deleted &&
                    !message.link_preview_checked &&
                    !this.previews.length &&
                    /https?:\/\//i.test(message.body_text || "")
                ) {
                    // The RPC only queues work. Native rows arrive through the
                    // same message_updated event as edits and downloaded media.
                    this.orm
                        .call("contact.center.ui.api", "request_link_previews", [
                            message.channel_id,
                            message.message_id,
                        ])
                        .catch(() => false);
                }
            },
            () => [this.props.message.message_id, this.props.message.body_text]
        );
    }

    get previews() {
        const message = this.props.message;
        const seen = new Set();
        if (message.is_deleted || !Array.isArray(message.link_previews)) {
            return [];
        }
        return message.link_previews
            .filter((preview) => {
                if (
                    !preview ||
                    !Number.isSafeInteger(preview.id) ||
                    seen.has(preview.id) ||
                    !safePreviewUrl(preview.source_url)
                ) {
                    return false;
                }
                seen.add(preview.id);
                return true;
            })
            .slice(0, 3)
            .map((preview) => ({
                id: preview.id,
                url: safePreviewUrl(preview.source_url),
                image: safePreviewUrl(
                    preview.og_image ||
                        (preview.image_mimetype ? preview.source_url : "")
                ),
                title: typeof preview.og_title === "string" ? preview.og_title : "",
                description:
                    typeof preview.og_description === "string"
                        ? preview.og_description
                        : "",
                hostname: new URL(preview.source_url).hostname,
            }));
    }
}

MessageLinkPreviews.template = "contact_center_ui.MessageLinkPreviews";
MessageLinkPreviews.props = {message: Object};
