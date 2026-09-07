/** @odoo-module **/

import {
    Component,
    onMounted,
    onWillDestroy,
    onWillStart,
    useRef,
    useState,
} from "@odoo/owl";
import {deserializeDate, formatDate} from "@web/core/l10n/dates";
import {formatFloat} from "@web/views/fields/formatters";
import {useService} from "@web/core/utils/hooks";
import {validateEnvelope} from "@contact_center_ui/js/contact_center_model.esm";

const PAGE_SIZE = 20;
export const CUSTOMER_TABS = [
    {id: "opportunities", label: "Oportunidades", model: "crm.lead"},
    {id: "quotations", label: "Cotações", model: "sale.order"},
    {id: "orders", label: "Pedidos", model: "sale.order"},
    {id: "invoices", label: "Faturas", model: "account.move"},
];

function record(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function reference(value) {
    return record(value) && Number.isSafeInteger(value.id) && value.id > 0
        ? {id: value.id, name: typeof value.name === "string" ? value.name : ""}
        : false;
}

function label(value) {
    return record(value) && typeof value.label === "string" ? value.label : "";
}

function normalizeCurrency(value) {
    if (!record(value)) {
        return false;
    }
    return {
        symbol: typeof value.symbol === "string" ? value.symbol : "",
        position: value.position === "after" ? "after" : "before",
        decimalPlaces:
            Number.isSafeInteger(value.decimal_places) &&
            value.decimal_places >= 0 &&
            value.decimal_places <= 16
                ? value.decimal_places
                : 2,
    };
}

function normalizeItem(item, tab) {
    const entry = reference(item);
    if (!entry || item.model !== tab.model) {
        throw new TypeError("Unexpected customer record model");
    }
    return {
        ...entry,
        model: item.model,
        state: label(item.state),
        amount:
            typeof item.amount === "number" && Number.isFinite(item.amount)
                ? item.amount
                : false,
        currency: normalizeCurrency(item.currency),
        date:
            typeof item.date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(item.date)
                ? item.date
                : false,
        stage: reference(item.stage),
        won: Boolean(record(item.stage) && item.stage.is_won === true),
        team: reference(item.team),
        user: reference(item.user),
        type: typeof item.type === "string" ? item.type : "",
        typeLabel: typeof item.type_label === "string" ? item.type_label : "",
        active: item.active !== false,
        paymentState: label(item.payment_state),
    };
}

function normalizeTabs(values) {
    if (!Array.isArray(values) || values.length !== CUSTOMER_TABS.length) {
        throw new TypeError("Invalid customer tabs");
    }
    return CUSTOMER_TABS.map((entry) => {
        const matches = values.filter(
            (value) => record(value) && value.id === entry.id
        );
        if (matches.length !== 1 || typeof matches[0].available !== "boolean") {
            throw new TypeError("Invalid customer tab availability");
        }
        return {...entry, available: matches[0].available};
    });
}

export function normalizeCustomerPage(payload, channelId, tabId) {
    validateEnvelope(payload);
    const tab = CUSTOMER_TABS.find((entry) => entry.id === tabId);
    if (
        !tab ||
        payload.channel_id !== channelId ||
        payload.tab !== tabId ||
        !["ready", "unavailable"].includes(payload.status) ||
        !Array.isArray(payload.items) ||
        typeof payload.has_more !== "boolean"
    ) {
        throw new TypeError("Unexpected customer records projection");
    }
    const tabs = normalizeTabs(payload.tabs);
    const available = tabs.find((entry) => entry.id === tabId).available;
    if (available !== (payload.status === "ready")) {
        throw new TypeError("Inconsistent customer tab availability");
    }
    const partner = reference(payload.partner);
    const canRead = Boolean(available && partner);
    const items = canRead ? payload.items.map((item) => normalizeItem(item, tab)) : [];
    const hasMore = Boolean(canRead && payload.has_more);
    if (hasMore && !items.length) {
        throw new TypeError("Customer pagination did not advance");
    }
    return {
        tabs,
        partner,
        company: reference(payload.commercial_partner),
        items,
        hasMore,
        phase: available ? "ready" : "unavailable",
    };
}

function emptyPage(query = "") {
    return {
        phase: "idle",
        items: [],
        query,
        appliedQuery: "",
        hasMore: false,
        nextOffset: 0,
        loadingMore: false,
        error: "",
    };
}

function customerKey(store) {
    const conversation = store.selectedConversation;
    const partner =
        conversation && conversation.identity && conversation.identity.partner;
    const company = partner && partner.company;
    return `${partner ? partner.id : 0}:${company ? company.id : 0}`;
}

function sameCustomer(left, right) {
    return (
        (left.partner && left.partner.id) === (right.partner && right.partner.id) &&
        (left.company && left.company.id) === (right.company && right.company.id)
    );
}

/** One panel owns its customer; each tab keeps its search and reloads on selection. */
export class CrmPanelModel {
    constructor({store, channelId, action, stateFactory = (state) => state}) {
        this.store = store;
        this.channelId = channelId;
        this.customerKey = customerKey(store);
        this.action = action;
        this.request = 0;
        this.destroyed = false;
        this.state = stateFactory({
            activeTab: "opportunities",
            tabs: CUSTOMER_TABS.map((tab) => ({...tab, available: null})),
            pages: Object.fromEntries(
                CUSTOMER_TABS.map((tab) => [tab.id, emptyPage()])
            ),
            partner: false,
            company: false,
            operationError: "",
        });
    }

    get page() {
        return this.state.pages[this.state.activeTab];
    }

    current() {
        return (
            !this.destroyed &&
            this.store.state.selectedChannelId === this.channelId &&
            this.store.capabilities.view_crm === true &&
            customerKey(this.store) === this.customerKey
        );
    }

    destroy() {
        this.destroyed = true;
        this.request += 1;
    }

    isCurrentRequest(request) {
        return this.current() && request === this.request;
    }

    selectTab(tabId) {
        if (
            !this.current() ||
            tabId === this.state.activeTab ||
            !CUSTOMER_TABS.some((tab) => tab.id === tabId)
        ) {
            return false;
        }
        this.request += 1;
        this.state.activeTab = tabId;
        this.state.operationError = "";
        this.state.pages[tabId] = emptyPage(this.page.query);
        if (this.state.tabs.find((tab) => tab.id === tabId).available === false) {
            this.page.phase = "unavailable";
            return true;
        }
        return this.load();
    }

    applyPage(projection, {append, offset, query}) {
        this.state.tabs = projection.tabs;
        this.state.partner = projection.partner;
        this.state.company = projection.company;
        const items =
            append && projection.phase === "ready" && projection.partner
                ? this.page.items
                : [];
        this.page.items = Array.from(
            new Map(
                [...items, ...projection.items].map((item) => [item.id, item])
            ).values()
        );
        this.page.hasMore = projection.hasMore;
        this.page.nextOffset = offset + projection.items.length;
        this.page.appliedQuery = query;
        this.page.phase = projection.phase;
    }

    failLoad(error, append) {
        if (error && error.data && error.data.name === "odoo.exceptions.AccessError") {
            this.state.partner = false;
            this.state.company = false;
            for (const tab of CUSTOMER_TABS) {
                this.state.pages[tab.id] = emptyPage();
            }
            this.page.phase = "denied";
        } else {
            this.page.phase = append ? "ready" : "error";
            this.page.error = "Não foi possível carregar os registros do cliente.";
        }
    }

    restartForCustomer(query) {
        this.state.partner = false;
        this.state.company = false;
        this.state.operationError = "";
        for (const tab of CUSTOMER_TABS) {
            this.state.pages[tab.id] = emptyPage(this.state.pages[tab.id].query);
        }
        this.page.appliedQuery = query;
        return this.load({restart: true});
    }

    async load({append = false, restart = false} = {}) {
        if (
            !this.current() ||
            (append && (!this.page.hasMore || this.page.loadingMore))
        ) {
            return false;
        }
        const request = ++this.request;
        const tabId = this.state.activeTab;
        const offset = append ? this.page.nextOffset : 0;
        const query =
            append || restart
                ? this.page.appliedQuery
                : this.page.query.trim().slice(0, 128);
        this.page.error = "";
        this.page.loadingMore = append;
        if (!append) {
            this.page.phase = "loading";
            this.page.items = [];
            this.page.hasMore = false;
            this.page.nextOffset = 0;
        }
        try {
            const response = await this.store.call("get_customer_records", [
                this.channelId,
                tabId,
                query,
                offset,
                PAGE_SIZE,
            ]);
            if (!this.isCurrentRequest(request)) {
                return false;
            }
            const projection = normalizeCustomerPage(response, this.channelId, tabId);
            if (append && !sameCustomer(projection, this.state)) {
                return this.restartForCustomer(query);
            }
            this.applyPage(projection, {
                append,
                offset,
                query,
            });
            return true;
        } catch (error) {
            if (!this.isCurrentRequest(request)) {
                return false;
            }
            this.failLoad(error, append);
            return false;
        } finally {
            if (this.isCurrentRequest(request)) {
                this.page.loadingMore = false;
            }
        }
    }

    async openRecord(recordId) {
        const item = this.page.items.find((entry) => entry.id === recordId);
        if (!this.current() || this.page.phase !== "ready" || !item) {
            return false;
        }
        const tabId = this.state.activeTab;
        this.state.operationError = "";
        try {
            await this.action.doAction(
                {
                    type: "ir.actions.act_window",
                    name: item.name,
                    res_model: item.model,
                    res_id: item.id,
                    views: [[false, "form"]],
                    view_mode: "form",
                    target: "new",
                },
                {
                    onClose: () =>
                        this.current() && this.state.activeTab === tabId
                            ? this.load()
                            : false,
                }
            );
            return true;
        } catch (_error) {
            if (this.current() && this.state.activeTab === tabId) {
                this.state.operationError = "Não foi possível abrir este registro.";
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

    get page() {
        return this.model.page;
    }

    amountLabel(item) {
        if (item.amount === false) {
            return "";
        }
        const currency = item.currency;
        const value = formatFloat(item.amount, {
            digits: [16, currency ? currency.decimalPlaces : 2],
        });
        if (!currency || !currency.symbol) {
            return value;
        }
        return currency.position === "after"
            ? `${value} ${currency.symbol}`
            : `${currency.symbol} ${value}`;
    }

    dateLabel(item) {
        if (!item.date) {
            return "";
        }
        const date = deserializeDate(item.date);
        return date.isValid ? formatDate(date) : "";
    }

    onTabKeydown(event) {
        const index = CUSTOMER_TABS.findIndex((tab) => tab.id === this.state.activeTab);
        const keys = {
            ArrowLeft: (index + 3) % 4,
            ArrowRight: (index + 1) % 4,
            Home: 0,
            End: 3,
        };
        if (Object.prototype.hasOwnProperty.call(keys, event.key)) {
            event.preventDefault();
            const tab = CUSTOMER_TABS[keys[event.key]];
            this.model.selectTab(tab.id);
            event.currentTarget.parentElement
                .querySelector(`#cc-crm-tab-${tab.id}`)
                .focus();
        }
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
