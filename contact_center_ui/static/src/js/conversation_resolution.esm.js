/** @odoo-module **/

import {Component, onWillDestroy, onWillStart, useState} from "@odoo/owl";
import {conversationDisplayName, makeClientRequestId} from "./contact_center_model.esm";
import {Dialog} from "@web/core/dialog/dialog";
import {useService} from "@web/core/utils/hooks";

export class ConversationResolution extends Component {
    setup() {
        this.action = useService("action");
        this.local = useState({
            loading: true,
            saving: false,
            items: [],
            canManage: false,
            reasonId: "",
            justification: "",
            revision: false,
            error: "",
        });
        this.destroyed = false;
        this.requestId = makeClientRequestId(this.props.store.operationCrypto);
        this.submittedValues = null;
        onWillStart(() => this.loadCatalog());
        onWillDestroy(() => {
            this.destroyed = true;
        });
    }

    get conversationName() {
        return conversationDisplayName(this.props.conversation);
    }

    get canConfirm() {
        return Boolean(
            !this.local.loading &&
                !this.local.saving &&
                this.local.revision &&
                this.local.items.some(
                    (item) => item.id === Number(this.local.reasonId)
                ) &&
                this.local.justification.trim() &&
                this.local.justification.trim().length <= 500
        );
    }

    async loadCatalog() {
        this.local.loading = true;
        this.local.error = "";
        try {
            const result = await this.props.store.call("resolution_reason_catalog", [
                this.props.conversation.channel_id,
            ]);
            if (this.destroyed) {
                return;
            }
            if (!Array.isArray(result.items) || typeof result.revision !== "string") {
                throw new Error("O cadastro de motivos não respondeu corretamente.");
            }
            this.local.items = result.items;
            this.local.canManage = result.can_manage === true;
            // Registering a reason must not silently renew the operator's
            // decision if a customer message arrived behind the modal.
            if (!this.local.revision) {
                this.local.revision = result.revision;
            }
            if (!result.items.some((item) => item.id === Number(this.local.reasonId))) {
                this.local.reasonId = "";
            }
        } catch (error) {
            if (!this.destroyed) {
                this.local.error = this.errorText(error);
            }
        } finally {
            if (!this.destroyed) {
                this.local.loading = false;
            }
        }
    }

    errorText(error) {
        return (
            (error.data && error.data.message) ||
            error.message ||
            "Não foi possível concluir. Cancele e confira a conversa antes de tentar novamente."
        );
    }

    async manageReasons() {
        if (!this.local.canManage || this.local.saving || this.submittedValues) {
            return;
        }
        await this.action.doAction(
            "contact_center_base.action_contact_center_resolution_reasons",
            {
                additionalContext: {dialog_size: "medium"},
                onClose: () => {
                    if (!this.destroyed) {
                        this.loadCatalog();
                    }
                },
            }
        );
    }

    async confirm() {
        if (!this.canConfirm) {
            return false;
        }
        // Retry the same decision after an uncertain network outcome. Never
        // silently accept a newer conversation revision or change the request.
        if (!this.submittedValues) {
            this.submittedValues = {
                reasonId: Number(this.local.reasonId),
                justification: this.local.justification.trim(),
                requestId: this.requestId,
                revision: this.local.revision,
            };
        }
        this.local.saving = true;
        this.local.error = "";
        try {
            await this.props.store.resolveConversation(
                this.props.conversation.channel_id,
                this.submittedValues
            );
            if (!this.destroyed) {
                this.props.close();
            }
            return true;
        } catch (error) {
            if (!this.destroyed) {
                this.local.error = this.errorText(error);
            }
            return false;
        } finally {
            if (!this.destroyed) {
                this.local.saving = false;
            }
        }
    }
}

ConversationResolution.components = {Dialog};
ConversationResolution.props = {close: Function, store: Object, conversation: Object};
ConversationResolution.template = "contact_center_ui.ConversationResolution";
