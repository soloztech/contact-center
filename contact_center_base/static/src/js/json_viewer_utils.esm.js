/** @odoo-module **/

/**
 * Helpers kept separate from the field component so formatting can be tested
 * without mounting a form view.
 */

export function isEmptyJsonValue(value) {
    return value === false || value === null || value === undefined || value === "";
}

export function formatJsonValue(value) {
    if (isEmptyJsonValue(value)) {
        return "";
    }
    try {
        const formatted = JSON.stringify(value, null, 2);
        return typeof formatted === "string" ? formatted : "";
    } catch (_error) {
        return "";
    }
}

export function jsonLineCount(value) {
    return value ? value.split("\n").length : 0;
}

export function jsonByteLength(value) {
    let bytes = 0;
    for (const character of value || "") {
        const codePoint = character.codePointAt(0);
        if (codePoint <= 0x7f) {
            bytes += 1;
        } else if (codePoint <= 0x7ff) {
            bytes += 2;
        } else if (codePoint <= 0xffff) {
            bytes += 3;
        } else {
            bytes += 4;
        }
    }
    return bytes;
}

export function formatJsonSize(bytes) {
    if (bytes < 1024) {
        return `${bytes} B`;
    }
    if (bytes < 1024 * 1024) {
        const digits = bytes < 10 * 1024 ? 1 : 0;
        return `${(bytes / 1024).toFixed(digits).replace(".", ",")} KB`;
    }
    return `${(bytes / (1024 * 1024)).toFixed(1).replace(".", ",")} MB`;
}
