/** @odoo-module **/

import {Component, onWillUnmount, useState} from "@odoo/owl";
import {_lt, _t} from "@web/core/l10n/translation";
import {
    formatJsonSize,
    formatJsonValue,
    jsonByteLength,
    jsonLineCount,
} from "./json_viewer_utils.esm";
import {browser} from "@web/core/browser/browser";
import {registry} from "@web/core/registry";
import {standardFieldProps} from "@web/views/fields/standard_field_props";
import {useService} from "@web/core/utils/hooks";

const COPY_STATE_RESET_DELAY = 1800;

async function copyText(value) {
    if (
        browser.navigator.clipboard &&
        typeof browser.navigator.clipboard.writeText === "function"
    ) {
        await browser.navigator.clipboard.writeText(value);
        return;
    }

    const textArea = document.createElement("textarea");
    textArea.value = value;
    textArea.setAttribute("readonly", "readonly");
    textArea.style.position = "fixed";
    textArea.style.opacity = "0";
    document.body.appendChild(textArea);
    textArea.select();
    try {
        if (!document.execCommand("copy")) {
            throw new Error("Clipboard copy failed");
        }
    } finally {
        textArea.remove();
    }
}

export class ContactCenterJsonViewerField extends Component {
    setup() {
        this.notification = useService("notification");
        this.state = useState({copyState: "idle"});
        onWillUnmount(() => this.clearCopyTimer());
    }

    get formattedValue() {
        return formatJsonValue(this.props.value);
    }

    get hasValue() {
        return Boolean(this.formattedValue);
    }

    get lineCount() {
        return jsonLineCount(this.formattedValue);
    }

    get sizeLabel() {
        return formatJsonSize(jsonByteLength(this.formattedValue));
    }

    get copyLabel() {
        if (this.state.copyState === "copied") {
            return _t("Copiado");
        }
        if (this.state.copyState === "error") {
            return _t("Falhou");
        }
        return _t("Copiar");
    }

    get copyIcon() {
        return this.state.copyState === "copied" ? "fa fa-check" : "fa fa-copy";
    }

    clearCopyTimer() {
        if (this.copyTimer) {
            browser.clearTimeout(this.copyTimer);
            this.copyTimer = null;
        }
    }

    scheduleCopyStateReset() {
        this.clearCopyTimer();
        this.copyTimer = browser.setTimeout(() => {
            this.state.copyState = "idle";
            this.copyTimer = null;
        }, COPY_STATE_RESET_DELAY);
    }

    async onCopy() {
        const value = this.formattedValue;
        if (!value) {
            return;
        }
        try {
            await copyText(value);
            this.state.copyState = "copied";
        } catch (_error) {
            this.state.copyState = "error";
            this.notification.add(_t("Não foi possível copiar o JSON."), {
                type: "danger",
            });
        }
        this.scheduleCopyStateReset();
    }
}

ContactCenterJsonViewerField.template =
    "contact_center_base.ContactCenterJsonViewerField";
ContactCenterJsonViewerField.props = {...standardFieldProps};
ContactCenterJsonViewerField.displayName = _lt("Visualizador JSON do Contact Center");
ContactCenterJsonViewerField.supportedTypes = ["json"];

registry
    .category("fields")
    .add("contact_center_json_viewer", ContactCenterJsonViewerField);
