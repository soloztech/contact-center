/** @odoo-module **/

import {Component, useRef, useState} from "@odoo/owl";
import {
    connectionFleetMeta,
    connectionHealthDetail,
    connectionHealthNeedsAttention,
    connectionHealthStateMeta,
} from "./contact_center_model.esm";
import {deserializeDateTime} from "@web/core/l10n/dates";

const {DateTime} = luxon;

export class ConnectionHealth extends Component {
    setup() {
        this.toggleRef = useRef("toggle");
        this.local = useState({view: this.attentionCount ? "attention" : "all"});
    }

    get state() {
        return this.props.state;
    }

    get store() {
        return this.props.store;
    }

    get health() {
        return this.store.connectionHealth;
    }

    get summary() {
        return this.health.summary;
    }

    get fleetMeta() {
        return connectionFleetMeta(this.health);
    }

    get attentionCount() {
        return this.health.items.filter(connectionHealthNeedsAttention).length;
    }

    get visibleItems() {
        if (this.local.view === "attention" && this.attentionCount) {
            return this.health.items.filter(connectionHealthNeedsAttention);
        }
        return this.health.items;
    }

    get canCheckAll() {
        return this.store.canCheckConnections || this.summary.can_check;
    }

    get checkAllDisabled() {
        return (
            this.state.connectionHealthPhase === "checking" ||
            (this.summary.total > 0 && this.summary.checking >= this.summary.total)
        );
    }

    stateMeta(state) {
        return connectionHealthStateMeta(state);
    }

    rowClass(item) {
        return [
            "cc-connection-row",
            `cc-connection-row--${item.state}`,
            item.checking ? "is-checking" : "",
        ]
            .filter(Boolean)
            .join(" ");
    }

    displayName(item) {
        return item.connection_name || item.account_name || `Conexão ${item.id}`;
    }

    checkLabel(item) {
        return `Verificar ${this.displayName(item)}`;
    }

    sourceLabel(item) {
        return [item.account_name, item.platform, item.provider]
            .filter(Boolean)
            .join(" · ");
    }

    roleLabel(item) {
        return (
            {
                primary: item.accepts_inbound ? "Principal · recebe" : "Principal",
                standby: "Reserva",
                migration: "Migração",
                historical: "Histórica",
            }[item.connection_role] || "Reserva"
        );
    }

    unsupportedLabel(item) {
        const count = Number(item.unsupported_count) || 0;
        return `${count} evento${count === 1 ? "" : "s"} não tratado${
            count === 1 ? "" : "s"
        } nas últimas 24 horas`;
    }

    detailLabel(item) {
        return item.state === "connected" && !connectionHealthNeedsAttention(item)
            ? ""
            : connectionHealthDetail(item);
    }

    canCheck(item) {
        return this.store.canCheckConnections || item.can_check;
    }

    relativeCheck(value) {
        if (!value) {
            return "Ainda não verificada";
        }
        try {
            const date = deserializeDateTime(value);
            if (!date || !date.isValid) {
                return "Horário indisponível";
            }
            const minutes = Math.max(
                0,
                Math.floor(DateTime.local().diff(date, "minutes").minutes)
            );
            if (minutes < 1) {
                return "Verificada agora";
            }
            if (minutes < 60) {
                return `Verificada há ${minutes} min`;
            }
            if (minutes < 24 * 60) {
                return `Verificada há ${Math.floor(minutes / 60)} h`;
            }
            return `Verificada em ${date.toFormat("dd/LL, HH:mm")}`;
        } catch (_error) {
            return "Horário indisponível";
        }
    }

    isChecking(item) {
        return (
            item.checking ||
            this.state.connectionHealthCheckingId === 0 ||
            this.state.connectionHealthCheckingId === item.id
        );
    }

    toggle() {
        if (!this.state.connectionHealthOpen && !this.attentionCount) {
            this.local.view = "all";
        }
        this.store.toggleConnectionHealth();
    }

    close({restoreFocus = true} = {}) {
        this.store.closeConnectionHealth();
        if (restoreFocus && this.toggleRef.el) {
            this.toggleRef.el.focus();
        }
    }

    onKeydown(event) {
        if (event.key === "Escape" && this.state.connectionHealthOpen) {
            event.preventDefault();
            event.stopPropagation();
            this.close();
        }
    }

    setView(view) {
        if (view === "all" || (view === "attention" && this.attentionCount)) {
            this.local.view = view;
        }
    }

    async checkAll() {
        await this.store.checkConnectionHealth(false);
    }

    async check(item) {
        await this.store.checkConnectionHealth(item.id);
    }
}

ConnectionHealth.props = {state: Object, store: Object};
ConnectionHealth.template = "contact_center_ui.ConnectionHealth";
