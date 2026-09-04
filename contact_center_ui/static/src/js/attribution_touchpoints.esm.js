/** @odoo-module **/

import {Component} from "@odoo/owl";
import {deserializeDateTime} from "@web/core/l10n/dates";

const TOUCHPOINT_LABELS = Object.freeze({
    paid_ad_click: "Anúncio confirmado",
    paid_ad_signal: "Sinal de mídia paga",
    entry_point: "Ponto de entrada",
    organic_link: "Link orgânico",
    unknown: "Origem observada",
});

const EVIDENCE_LABELS = Object.freeze({
    provider_asserted: "Confirmado pelo canal",
    provider_asserted_non_paid: "Entrada não paga",
    provider_hint: "Sinal do canal",
    observed: "Observado",
    derived: "Derivado",
});

export function attributionProjectionVisible(projection) {
    const hasItems = Boolean(
        projection && Array.isArray(projection.items) && projection.items.length
    );
    return Boolean(
        projection &&
            ((projection.enabled === true && hasItems) ||
                projection.phase === "loading" ||
                projection.phase === "error")
    );
}

export function attributionOccurredAt(value) {
    try {
        const date = deserializeDateTime(value);
        if (!date.isValid) {
            return {datetime: "", label: ""};
        }
        return {
            datetime: date.toUTC().toISO({suppressMilliseconds: true}) || "",
            label: date.toFormat("dd/LL/yyyy 'às' HH:mm"),
        };
    } catch (_error) {
        return {datetime: "", label: ""};
    }
}

export class AttributionTouchpoints extends Component {
    get store() {
        return this.props.store;
    }

    get projection() {
        return this.props.attribution || {enabled: false, phase: "idle", items: []};
    }

    get items() {
        return Array.isArray(this.projection.items) ? this.projection.items : [];
    }

    get visible() {
        return attributionProjectionVisible(this.projection);
    }

    retry() {
        return this.store.loadAttribution({
            silent: false,
            preserve: Boolean(this.items.length),
        });
    }

    typeLabel(item) {
        return TOUCHPOINT_LABELS[item.touchpoint_type] || "Origem observada";
    }

    evidenceLabel(item) {
        return EVIDENCE_LABELS[item.evidence_level] || "Observado";
    }

    evidenceTone(item) {
        if (item.evidence_level === "provider_asserted") {
            return "confirmed";
        }
        if (item.evidence_level === "provider_hint") {
            return "hint";
        }
        return "neutral";
    }

    sourceLabel(item) {
        const source = item.utm_campaign || item.entry_point_source || item.source_type;
        const platform = item.source_platform || item.entry_point_app || item.network;
        if (source && platform && source !== platform) {
            return `${source} · ${platform}`;
        }
        return source || platform || "Origem não detalhada";
    }

    occurredAtLabel(item) {
        return attributionOccurredAt(item.occurred_at).label;
    }

    occurredAtDateTime(item) {
        return attributionOccurredAt(item.occurred_at).datetime;
    }
}

AttributionTouchpoints.props = {attribution: Object, store: Object};
AttributionTouchpoints.template = "contact_center_ui.AttributionTouchpoints";
