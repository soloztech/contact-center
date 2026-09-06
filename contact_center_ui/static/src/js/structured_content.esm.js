/** @odoo-module **/

import {makeClientRequestId} from "./contact_center_model.esm";

export const STRUCTURED_CONTENT_LABELS = Object.freeze({
    buttons: "Botões",
    list: "Lista de opções",
    contacts: "Contato",
    location: "Localização",
});

export const STRUCTURED_ACTION_LABELS = Object.freeze({
    reply: "Responder",
    url: "Abrir link",
    phone: "Ligar",
});

// These are DTO bounds, not provider defaults. Every channel must declare its
// complete spec within the same bounds enforced by the application contract.
const SPEC_LIMITS = Object.freeze({
    buttons: {
        max_buttons: [1, 25],
        max_title_length: [0, 512],
        max_footer_length: [0, 512],
        max_button_title_length: [1, 200],
        max_id_length: [1, 512],
        max_phone_length: [1, 64],
    },
    list: {
        max_sections: [1, 20],
        max_rows: [1, 100],
        max_title_length: [0, 512],
        max_footer_length: [0, 512],
        max_button_text_length: [1, 200],
        max_section_title_length: [1, 512],
        max_row_title_length: [1, 200],
        max_row_description_length: [0, 1000],
        max_id_length: [1, 512],
    },
    contacts: {
        max_contacts: [1, 50],
        max_name_length: [1, 512],
        max_phones: [0, 20],
        max_emails: [0, 20],
        max_phone_length: [1, 64],
        max_email_length: [1, 254],
    },
    location: {max_name_length: [0, 512], max_address_length: [0, 2000]},
});

function plainRecord(value) {
    return (
        value !== null &&
        typeof value === "object" &&
        !Array.isArray(value) &&
        [Object.prototype, null].includes(Object.getPrototypeOf(value))
    );
}

function validStructuredBodySpec(spec) {
    return (
        ["required", "optional", "none"].includes(spec.body_mode) &&
        Number.isSafeInteger(spec.max_body_length) &&
        spec.max_body_length >= 0 &&
        spec.max_body_length <= 65536 &&
        (spec.body_mode === "none") === (spec.max_body_length === 0)
    );
}

function validStructuredSpec(type, spec) {
    if (!plainRecord(spec) || !validStructuredBodySpec(spec)) {
        return false;
    }
    const limits = SPEC_LIMITS[type];
    const extra =
        type === "buttons"
            ? ["action_types"]
            : type === "location"
            ? ["allow_live"]
            : [];
    const allowed = [...Object.keys(limits), ...extra, "body_mode", "max_body_length"];
    if (
        Object.keys(spec).length !== allowed.length ||
        Object.keys(spec).some((key) => !allowed.includes(key)) ||
        Object.entries(limits).some(
            ([key, [minimum, maximum]]) =>
                !Number.isSafeInteger(spec[key]) ||
                spec[key] < minimum ||
                spec[key] > maximum
        )
    ) {
        return false;
    }
    if (type === "buttons") {
        return (
            Array.isArray(spec.action_types) &&
            spec.action_types.length > 0 &&
            new Set(spec.action_types).size === spec.action_types.length &&
            spec.action_types.every((action) =>
                Object.keys(STRUCTURED_ACTION_LABELS).includes(action)
            )
        );
    }
    return type !== "location" || typeof spec.allow_live === "boolean";
}

export function outboundStructuredCapabilities(capabilities) {
    const source =
        plainRecord(capabilities) && capabilities.outbound_structured_content;
    if (!plainRecord(source)) {
        return {};
    }
    const types = Object.keys(STRUCTURED_CONTENT_LABELS);
    if (
        Object.entries(source).some(
            ([type, spec]) => !types.includes(type) || !validStructuredSpec(type, spec)
        )
    ) {
        return {};
    }
    return {...source};
}

export function safeStructuredUrl(value) {
    if (typeof value !== "string") {
        return "";
    }
    try {
        const url = new URL(value);
        const host = url.hostname.toLowerCase();
        if (
            url.protocol !== "https:" ||
            url.username ||
            url.password ||
            url.port ||
            /[\s\\]/.test(value) ||
            [...url.searchParams.keys()].some((key) =>
                /token|secret|signature|credential/i.test(key)
            ) ||
            !host.includes(".") ||
            host.endsWith(".localhost") ||
            host.endsWith(".local") ||
            /^\d+\.\d+\.\d+\.\d+$/.test(host)
        ) {
            return "";
        }
        return url.href;
    } catch (_error) {
        return "";
    }
}

export function newStructuredRow(actionType, maxIdLength, rows = []) {
    let ordinal = 1;
    const compactId = (index) =>
        maxIdLength === 1 ? String.fromCodePoint(0x100 + index) : index.toString(36);
    while (rows.some((row) => row.id === compactId(ordinal))) {
        ordinal += 1;
    }
    return {
        id: maxIdLength >= 36 ? makeClientRequestId() : compactId(ordinal),
        type: actionType,
        title: "",
        description: "",
        url: "",
        phone: "",
    };
}

export function newStructuredDraft(type, capabilities) {
    const spec =
        Object.keys(STRUCTURED_CONTENT_LABELS).includes(type) &&
        outboundStructuredCapabilities(capabilities)[type];
    if (!spec) {
        return false;
    }
    return {
        type,
        title: "",
        footer: "",
        buttonText:
            type === "list" ? "Ver opções".slice(0, spec.max_button_text_length) : "",
        sectionTitle:
            type === "list" ? "Opções".slice(0, spec.max_section_title_length) : "",
        rows: ["buttons", "list"].includes(type)
            ? [
                  newStructuredRow(
                      type === "buttons" ? spec.action_types[0] : "reply",
                      spec.max_id_length
                  ),
              ]
            : [],
        name: "",
        phones: "",
        emails: "",
        latitude: "",
        longitude: "",
        address: "",
    };
}

function lines(value) {
    return value
        .split(/\n/)
        .map((item) => item.trim())
        .filter(Boolean);
}

function error(message) {
    return {content: false, error: message};
}

// Provider limits count Unicode code points, matching Python len and WhatsApp runes.
function countCodePoints(value) {
    return Array.from(value).length;
}

function validPhone(phone, maximum) {
    return (
        countCodePoints(phone) <= maximum &&
        /^\+?[0-9 ()\-.]+$/.test(phone) &&
        /[0-9]/.test(phone)
    );
}

function buttonsSubmission(draft, spec) {
    const {type} = draft;
    const title = draft.title.trim();
    const footer = draft.footer.trim();
    if (!draft.rows.length || draft.rows.length > spec.max_buttons) {
        return error(`Adicione de 1 a ${spec.max_buttons} botões.`);
    }
    const buttons = [];
    for (const row of draft.rows) {
        const label = row.title.trim();
        if (!label || countCodePoints(label) > spec.max_button_title_length) {
            return error(
                `Informe o texto de cada botão, com até ${spec.max_button_title_length} caracteres.`
            );
        }
        if (!spec.action_types.includes(row.type)) {
            return error(
                "Este canal não permite a ação de um dos botões. Escolha uma ação disponível."
            );
        }
        if (row.type === "url") {
            const url = safeStructuredUrl(row.url);
            if (!url || countCodePoints(url) > 2048) {
                return error("Informe um endereço HTTPS público para o botão.");
            }
            buttons.push({type: "url", title: label, url});
        } else if (row.type === "phone") {
            const phone = row.phone.trim();
            if (!validPhone(phone, spec.max_phone_length)) {
                return error(
                    `Informe um telefone válido de até ${spec.max_phone_length} caracteres para o botão.`
                );
            }
            buttons.push({type: "phone", title: label, phone});
        } else if (row.type === "reply") {
            if (!row.id || countCodePoints(row.id) > spec.max_id_length) {
                return error(
                    "O identificador de um dos botões excede o limite deste canal. Remova e crie a opção novamente."
                );
            }
            buttons.push({type: "reply", id: row.id, title: label});
        }
    }
    return {content: {type, title, footer, buttons}, error: ""};
}

function listSubmission(draft, spec) {
    const {type} = draft;
    const title = draft.title.trim();
    const footer = draft.footer.trim();
    const buttonText = draft.buttonText.trim();
    const sectionTitle = draft.sectionTitle.trim();
    if (
        !buttonText ||
        countCodePoints(buttonText) > spec.max_button_text_length ||
        !sectionTitle ||
        countCodePoints(sectionTitle) > spec.max_section_title_length
    ) {
        return error(
            `Informe o texto do botão (até ${spec.max_button_text_length} caracteres) e da seção (até ${spec.max_section_title_length}).`
        );
    }
    if (!draft.rows.length || draft.rows.length > spec.max_rows) {
        return error(`Adicione de 1 a ${spec.max_rows} opções.`);
    }
    const rows = draft.rows.map((row) => ({
        id: row.id,
        title: row.title.trim(),
        description: row.description.trim(),
    }));
    if (
        rows.some(
            (row) =>
                !row.title ||
                countCodePoints(row.title) > spec.max_row_title_length ||
                countCodePoints(row.description) > spec.max_row_description_length
        )
    ) {
        return error(
            `Cada opção precisa de um título de até ${spec.max_row_title_length} caracteres e descrição de até ${spec.max_row_description_length}.`
        );
    }
    if (rows.some((row) => !row.id || countCodePoints(row.id) > spec.max_id_length)) {
        return error(
            "O identificador de uma opção excede o limite deste canal. Remova e crie a opção novamente."
        );
    }
    return {
        content: {
            type,
            title,
            footer,
            button_text: buttonText,
            sections: [{title: sectionTitle, rows}],
        },
        error: "",
    };
}

function contactsSubmission(draft, spec) {
    const name = draft.name.trim();
    const phones = lines(draft.phones);
    const emails = lines(draft.emails);
    if (!name || countCodePoints(name) > spec.max_name_length) {
        return error(
            `Informe o nome do contato, com até ${spec.max_name_length} caracteres.`
        );
    }
    if (
        phones.length > spec.max_phones ||
        emails.length > spec.max_emails ||
        phones.some((phone) => !validPhone(phone, spec.max_phone_length)) ||
        emails.some(
            (email) =>
                countCodePoints(email) > spec.max_email_length ||
                !/^[^@\s?&#<>]+@[^@\s?&#<>]+\.[^@\s?&#<>]+$/.test(email)
        )
    ) {
        return error(
            `Revise os telefones (até ${spec.max_phones}, ${spec.max_phone_length} caracteres cada) e e-mails (até ${spec.max_emails}, ${spec.max_email_length} caracteres cada), um por linha.`
        );
    }
    return {content: {type: draft.type, contacts: [{name, phones, emails}]}, error: ""};
}

function locationSubmission(draft, spec) {
    const latitude = Number(draft.latitude);
    const longitude = Number(draft.longitude);
    if (
        !draft.latitude.trim() ||
        !draft.longitude.trim() ||
        !Number.isFinite(latitude) ||
        !Number.isFinite(longitude) ||
        Math.abs(latitude) > 90 ||
        Math.abs(longitude) > 180
    ) {
        return error("Informe latitude entre −90 e 90 e longitude entre −180 e 180.");
    }
    if (
        countCodePoints(draft.name.trim()) > spec.max_name_length ||
        countCodePoints(draft.address.trim()) > spec.max_address_length
    ) {
        return error(
            `O local aceita nome de até ${spec.max_name_length} caracteres e endereço de até ${spec.max_address_length}.`
        );
    }
    return {
        content: {
            type: draft.type,
            latitude,
            longitude,
            name: draft.name.trim(),
            address: draft.address.trim(),
        },
        error: "",
    };
}

export function structuredDraftSubmission(draft, body, capabilities) {
    if (!draft) {
        return {content: false, error: ""};
    }
    const text = body.trim();
    const type = draft.type;
    const spec =
        Object.keys(STRUCTURED_CONTENT_LABELS).includes(type) &&
        outboundStructuredCapabilities(capabilities)[type];
    if (!spec) {
        return error("Este cartão não está disponível nesta conversa.");
    }
    if (spec.body_mode === "none" && text) {
        return error(
            "Envie ou remova o texto antes de enviar este cartão; ele não aceita legenda."
        );
    }
    if (
        (spec.body_mode === "required" && !text) ||
        countCodePoints(text) > spec.max_body_length
    ) {
        return error(
            `A mensagem ${
                spec.body_mode === "required" ? "é obrigatória e " : ""
            }aceita até ${spec.max_body_length} caracteres neste canal.`
        );
    }
    if (
        ["buttons", "list"].includes(type) &&
        (countCodePoints(draft.title.trim()) > spec.max_title_length ||
            countCodePoints(draft.footer.trim()) > spec.max_footer_length)
    ) {
        return error(
            `O título aceita até ${spec.max_title_length} caracteres e o rodapé até ${spec.max_footer_length}.`
        );
    }
    const validate = {
        buttons: buttonsSubmission,
        list: listSubmission,
        contacts: contactsSubmission,
        location: locationSubmission,
    }[type];
    return validate(draft, spec);
}

export function structuredMessageCard(value) {
    if (!value || typeof value !== "object") {
        return false;
    }
    const card = {...value};
    if (card.type === "buttons") {
        card.buttons = (card.buttons || []).map((button) => ({
            ...button,
            href: button.type === "url" ? safeStructuredUrl(button.url) : "",
        }));
    } else if (card.type === "shared") {
        card.items = (card.items || []).map((item) => ({
            ...item,
            href: safeStructuredUrl(item.url),
        }));
    } else if (card.type === "location") {
        const latitude = card.latitude;
        const longitude = card.longitude;
        card.href =
            typeof latitude === "number" &&
            typeof longitude === "number" &&
            Number.isFinite(latitude) &&
            Number.isFinite(longitude) &&
            Math.abs(latitude) <= 90 &&
            Math.abs(longitude) <= 180
                ? `https://www.openstreetmap.org/?mlat=${latitude}&mlon=${longitude}#map=16/${latitude}/${longitude}`
                : "";
    } else if (!["list", "contacts", "selection"].includes(card.type)) {
        return false;
    }
    return card;
}
