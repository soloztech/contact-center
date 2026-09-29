/** @odoo-module **/

import {Component, onWillDestroy, onWillStart, useState} from "@odoo/owl";
import {Dialog} from "@web/core/dialog/dialog";
import {_t} from "@web/core/l10n/translation";
import {openContactCenterChat} from "@contact_center_ui/js/contact_center_messaging.esm";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";
import {validateEnvelope} from "@contact_center_ui/js/contact_center_model.esm";

const OPEN_ERROR = _t(
    "Não foi possível abrir a conversa. Confira seu acesso e tente novamente."
);
const LIST_ERROR = _t("Não foi possível carregar os atendimentos. Tente novamente.");

async function openConversation(env, orderId, channelId, isAlive = () => true) {
    const messaging = await env.services.messaging.get();
    if (!isAlive()) {
        return false;
    }
    const result = await env.services.orm.call(
        "sale.order",
        "open_contact_center_conversation",
        [[orderId], channelId]
    );
    if (!isAlive()) {
        return false;
    }
    validateEnvelope(result);
    openContactCenterChat(messaging, result.item);
    return true;
}

export class SaleConversationsDialog extends Component {
    setup() {
        this.orm = useService("orm");
        this.alive = true;
        this.state = useState({
            items: [],
            total: 0,
            offset: 0,
            limit: 50,
            has_more: false,
            loading: false,
            opening: false,
            error: "",
            ...this.props.initial,
        });
        onWillDestroy(() => {
            this.alive = false;
        });
        onWillStart(() => (this.props.initial ? undefined : this.load(0)));
    }

    isAlive() {
        // The dialog onClose callback fires synchronously for footer, header and Escape;
        // Owl's onWillDestroy alone runs too late for a same-tick RPC result.
        return this.alive && !this.props.closeState.closed;
    }

    async load(offset = this.state.offset) {
        if (!this.isAlive() || this.state.loading || this.state.opening) {
            return;
        }
        this.state.loading = true;
        this.state.error = "";
        this.state.items = [];
        try {
            const result = await this.orm.call(
                "sale.order",
                "get_contact_center_conversations",
                [[this.props.orderId]],
                {offset, limit: 50}
            );
            if (this.isAlive()) {
                Object.assign(this.state, result);
            }
        } catch {
            if (this.isAlive()) {
                this.state.error = LIST_ERROR;
            }
        } finally {
            if (this.isAlive()) {
                this.state.loading = false;
            }
        }
    }

    async open(row) {
        if (
            !this.isAlive() ||
            !row.can_open ||
            !row.channel_id ||
            this.state.opening ||
            this.state.loading
        ) {
            return;
        }
        this.state.opening = true;
        this.state.error = "";
        try {
            if (
                await openConversation(
                    this.env,
                    this.props.orderId,
                    row.channel_id,
                    () => this.isAlive()
                )
            ) {
                this.props.close();
            }
        } catch {
            if (this.isAlive()) {
                this.state.opening = false;
                await this.load(0);
                if (this.isAlive()) {
                    this.state.error = OPEN_ERROR;
                }
            }
        } finally {
            if (this.isAlive()) {
                this.state.opening = false;
            }
        }
    }
}
SaleConversationsDialog.template = "contact_center_sale.ConversationsDialog";
SaleConversationsDialog.components = {Dialog};
SaleConversationsDialog.props = {
    orderId: Number,
    initial: {type: Object, optional: true},
    closeState: Object,
    close: Function,
};

function addConversationsDialog(env, props) {
    const closeState = {closed: false};
    env.services.dialog.add(
        SaleConversationsDialog,
        {...props, closeState},
        {
            onClose: () => {
                closeState.closed = true;
            },
        }
    );
}

export async function showSaleConversations(env, action) {
    const orderId = action.params.order_id;
    try {
        const result = await env.services.orm.call(
            "sale.order",
            "get_contact_center_conversations",
            [[orderId]]
        );
        if (result.total === 1 && result.items[0].can_open) {
            try {
                await openConversation(env, orderId, result.items[0].channel_id);
                return;
            } catch {
                env.services.notification.add(OPEN_ERROR, {type: "warning"});
                addConversationsDialog(env, {orderId});
                return;
            }
        }
        addConversationsDialog(env, {orderId, initial: result});
    } catch {
        env.services.notification.add(LIST_ERROR, {type: "warning"});
        addConversationsDialog(env, {orderId});
    }
    // Functional action: never replace the sales controller or await dialog close.
}

registry
    .category("actions")
    .add("contact_center_sale.conversations", showSaleConversations);
