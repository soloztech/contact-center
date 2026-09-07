/** @odoo-module **/

import {
    Component,
    onMounted,
    onWillDestroy,
    onWillStart,
    useRef,
    useState,
} from "@odoo/owl";
import {formatFloat} from "@web/views/fields/formatters";
import {useService} from "@web/core/utils/hooks";
import {validateEnvelope} from "@contact_center_ui/js/contact_center_model.esm";

const PAGE_SIZE = 20;

function record(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function reference(value) {
    return record(value) && Number.isSafeInteger(value.id) && value.id > 0
        ? {id: value.id, name: typeof value.name === "string" ? value.name : ""}
        : false;
}

export function normalizeCrmPage(payload, channelId) {
    validateEnvelope(payload);
    if (payload.channel_id !== channelId || !record(payload.capabilities)) {
        throw new TypeError("Unexpected CRM conversation projection");
    }
    const capabilities = {
        view: payload.capabilities.view === true,
        link: payload.capabilities.link === true,
        unlink: payload.capabilities.unlink === true,
    };
    if (!capabilities.view) {
        return {
            capabilities,
            partner: false,
            company: false,
            items: [],
            hasMore: false,
        };
    }
    if (!Array.isArray(payload.items) || typeof payload.has_more !== "boolean") {
        throw new TypeError("Invalid CRM opportunity page");
    }
    const items = payload.items.map((item) => {
        const opportunity = reference(item);
        if (!opportunity || typeof item.linked !== "boolean") {
            throw new TypeError("Invalid CRM opportunity");
        }
        return {
            ...opportunity,
            linked: item.linked,
            type: item.type === "lead" ? "lead" : "opportunity",
            active: item.active !== false,
            stage: reference(item.stage),
            won: Boolean(record(item.stage) && item.stage.is_won === true),
            team: reference(item.team),
            user: reference(item.user),
            revenue:
                typeof item.expected_revenue === "number" &&
                Number.isFinite(item.expected_revenue)
                    ? item.expected_revenue
                    : false,
            currency: record(item.currency)
                ? {
                      symbol:
                          typeof item.currency.symbol === "string"
                              ? item.currency.symbol
                              : "",
                      position: item.currency.position === "after" ? "after" : "before",
                  }
                : false,
        };
    });
    if (payload.has_more && !items.length) {
        throw new TypeError("CRM pagination did not advance");
    }
    return {
        capabilities,
        partner: reference(payload.partner),
        company: reference(payload.commercial_partner),
        items,
        hasMore: payload.has_more,
    };
}

/** One mounted panel owns one conversation and discards late responses. */
export class CrmPanelModel {
    constructor({store, channelId, action, stateFactory = (state) => state}) {
        this.store = store;
        this.channelId = channelId;
        this.action = action;
        this.request = 0;
        this.destroyed = false;
        this.state = stateFactory({
            phase: "idle",
            items: [],
            partner: false,
            company: false,
            capabilities: {view: false, link: false, unlink: false},
            query: "",
            appliedQuery: "",
            hasMore: false,
            nextOffset: 0,
            loadingMore: false,
            error: "",
            operationError: "",
            busyId: false,
        });
    }

    current() {
        return (
            !this.destroyed &&
            this.store.state.selectedChannelId === this.channelId &&
            this.store.capabilities.view_crm === true
        );
    }

    destroy() {
        this.destroyed = true;
        this.request += 1;
    }

    applyPage(page, {append, offset, query}) {
        this.state.capabilities = page.capabilities;
        this.state.partner = page.partner;
        this.state.company = page.company;
        const items = append && page.capabilities.view ? this.state.items : [];
        this.state.items = Array.from(
            new Map([...items, ...page.items].map((item) => [item.id, item])).values()
        );
        this.state.hasMore = page.hasMore;
        this.state.nextOffset = offset + page.items.length;
        this.state.appliedQuery = query;
        this.state.phase = page.capabilities.view ? "ready" : "denied";
    }

    failLoad(error, append) {
        const denied =
            error && error.data && error.data.name === "odoo.exceptions.AccessError";
        if (denied) {
            this.state.items = [];
            this.state.partner = false;
            this.state.company = false;
            this.state.capabilities = {view: false, link: false, unlink: false};
            this.state.hasMore = false;
            this.state.phase = "denied";
        } else {
            this.state.phase = append ? "ready" : "error";
            this.state.error = "Não foi possível carregar as oportunidades.";
        }
    }

    async load({append = false} = {}) {
        if (
            !this.current() ||
            (append && (!this.state.hasMore || this.state.loadingMore))
        ) {
            return false;
        }
        const request = ++this.request;
        const offset = append ? this.state.nextOffset : 0;
        const query = append
            ? this.state.appliedQuery
            : this.state.query.trim().slice(0, 128);
        this.state.error = "";
        this.state.loadingMore = append;
        if (!append) {
            this.state.phase = "loading";
            this.state.items = [];
            this.state.hasMore = false;
            this.state.nextOffset = 0;
        }
        try {
            const response = await this.store.call("get_crm_opportunities", [
                this.channelId,
                query,
                offset,
                PAGE_SIZE,
            ]);
            if (!this.current() || request !== this.request) {
                return false;
            }
            const page = normalizeCrmPage(response, this.channelId);
            this.applyPage(page, {append, offset, query});
            return true;
        } catch (error) {
            if (!this.current() || request !== this.request) {
                return false;
            }
            this.failLoad(error, append);
            return false;
        } finally {
            if (this.current() && request === this.request) {
                this.state.loadingMore = false;
            }
        }
    }

    canChangeLink(opportunityId, linked) {
        const item = this.state.items.find((entry) => entry.id === opportunityId);
        return Boolean(
            this.current() &&
                this.state.phase === "ready" &&
                !this.state.busyId &&
                typeof linked === "boolean" &&
                item &&
                item.linked !== linked &&
                this.state.capabilities[linked ? "link" : "unlink"] === true
        );
    }

    async setLinked(opportunityId, linked) {
        if (!this.canChangeLink(opportunityId, linked)) {
            return false;
        }
        this.state.busyId = opportunityId;
        this.state.operationError = "";
        let accepted = false;
        try {
            const payload = await this.store.call(
                linked ? "link_crm_opportunity" : "unlink_crm_opportunity",
                [this.channelId, opportunityId]
            );
            validateEnvelope(payload);
            if (
                payload.channel_id !== this.channelId ||
                payload.opportunity_id !== opportunityId ||
                payload.linked !== linked
            ) {
                throw new TypeError("Unexpected CRM link acknowledgement");
            }
            accepted = true;
        } catch (_error) {
            if (this.current()) {
                this.state.operationError =
                    "Não foi possível confirmar a alteração. Confira o vínculo na lista atualizada.";
            }
        } finally {
            if (this.current()) {
                // Read back after an ambiguous response; never repeat a write automatically.
                await this.load();
                this.state.busyId = false;
            }
        }
        return accepted;
    }

    async openOpportunity(opportunityId) {
        const item = this.state.items.find((entry) => entry.id === opportunityId);
        if (!this.current() || !this.state.capabilities.view || !item) {
            return false;
        }
        this.state.operationError = "";
        try {
            await this.action.doAction(
                {
                    type: "ir.actions.act_window",
                    name: item.name,
                    res_model: "crm.lead",
                    res_id: item.id,
                    views: [[false, "form"]],
                    view_mode: "form",
                    target: "new",
                },
                {onClose: () => this.load()}
            );
            return true;
        } catch (_error) {
            if (this.current()) {
                this.state.operationError = "Não foi possível abrir esta oportunidade.";
            }
            return false;
        }
    }
}

export class CrmPanel extends Component {
    setup() {
        this.model = new CrmPanelModel({
            store: this.props.store,
            channelId: this.props.channelId,
            action: useService("action"),
            stateFactory: useState,
        });
        this.state = this.model.state;
        this.closeButton = useRef("closeButton");
        onMounted(() => this.closeButton.el.focus());
        onWillStart(() => {
            this.model.load();
        });
        onWillDestroy(() => this.model.destroy());
    }

    revenueLabel(item) {
        if (item.revenue === false) {
            return "";
        }
        const value = formatFloat(item.revenue, {digits: [16, 2]});
        const currency = item.currency;
        if (!currency || !currency.symbol) {
            return value;
        }
        return currency.position === "after"
            ? `${value} ${currency.symbol}`
            : `${currency.symbol} ${value}`;
    }

    search() {
        if (!this.state.busyId) {
            return this.model.load();
        }
        return false;
    }

    onKeydown(event) {
        if (event.key === "Escape") {
            event.stopPropagation();
            this.props.onClose();
        }
    }
}

CrmPanel.props = {store: Object, channelId: Number, onClose: Function};
CrmPanel.template = "contact_center_crm.CrmPanel";
