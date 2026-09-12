/** @odoo-module **/

import {Component, onWillDestroy, useEffect, useRef, useState} from "@odoo/owl";

export function retentionPolicyKey(policy) {
    if (!policy) {
        return "";
    }
    return JSON.stringify([
        policy.enabled,
        policy.preserve,
        policy.days,
        policy.can_manage,
        policy.revision,
    ]);
}

export function retentionDescription(policy) {
    if (!policy) {
        return "Consulte a regra da caixa para este grupo.";
    }
    if (policy.preserve) {
        return policy.enabled
            ? "Histórico preservado. Este grupo está isento da exclusão automática da caixa."
            : "Exclusão automática desativada nesta caixa. Este grupo continuará preservado se a regra for ativada.";
    }
    return policy.enabled
        ? `Mensagens e mídias deste grupo com mais de ${policy.days} ${
              policy.days === 1 ? "dia" : "dias"
          } serão excluídas da Central. O cadastro do grupo será mantido.`
        : "Exclusão automática desativada nesta caixa.";
}

export class HistoryRetention extends Component {
    setup() {
        this.section = useRef("section");
        this.sequence = 0;
        this.alive = true;
        this.local = useState({
            policy: this.props.policy || false,
            phase: "idle",
            error: "",
            notice: "",
            preview: false,
        });
        useEffect(
            () => {
                if (this.props.visible) {
                    if (
                        this.local.phase === "idle" ||
                        retentionPolicyKey(this.props.policy) !==
                            retentionPolicyKey(this.local.policy)
                    ) {
                        this.load();
                    }
                } else {
                    this.sequence++;
                    this.local.preview = false;
                    this.local.phase = "idle";
                }
            },
            () => [
                this.props.channelId,
                this.props.visible,
                retentionPolicyKey(this.props.policy),
            ]
        );
        useEffect(
            () => {
                if (this.props.visible && this.props.focusRequest && this.section.el) {
                    this.section.el.scrollIntoView({
                        block: "start",
                        behavior: "smooth",
                    });
                    this.section.el.focus({preventScroll: true});
                }
            },
            () => [this.props.visible, this.props.focusRequest]
        );
        onWillDestroy(() => {
            this.alive = false;
            this.sequence++;
        });
    }

    get busy() {
        return ["loading", "previewing", "saving"].includes(this.local.phase);
    }

    get canManage() {
        return Boolean(
            this.alive &&
                this.props.visible &&
                this.local.policy &&
                this.local.policy.can_manage &&
                this.local.phase === "ready"
        );
    }

    get description() {
        return retentionDescription(this.local.policy);
    }

    current(sequence, channelId) {
        return (
            this.alive &&
            sequence === this.sequence &&
            this.props.channelId === channelId &&
            this.props.visible
        );
    }

    async load(preview = false) {
        const channelId = this.props.channelId;
        const sequence = ++this.sequence;
        this.local.phase = preview ? "previewing" : "loading";
        this.local.error = "";
        this.local.notice = "";
        this.local.preview = false;
        try {
            const result = await this.props.store.getRetentionPolicy(
                channelId,
                preview
            );
            if (!this.current(sequence, channelId)) {
                return false;
            }
            this.local.policy = result.policy;
            this.local.preview = preview ? result.preview : false;
            this.local.phase = "ready";
            return true;
        } catch (_error) {
            if (this.current(sequence, channelId)) {
                this.local.phase = "error";
                this.local.error = preview
                    ? "Não foi possível simular a retomada da exclusão. A preservação não foi alterada."
                    : "Não foi possível confirmar a regra atual. Tente novamente.";
            }
            return false;
        }
    }

    async togglePreserve() {
        if (!this.canManage) {
            return false;
        }
        const policy = this.local.policy;
        if (policy.preserve && policy.enabled) {
            return this.load(true);
        }
        return this.save(!policy.preserve);
    }

    cancelPreview() {
        if (!this.busy) {
            this.local.preview = false;
        }
    }

    async confirmResume() {
        if (!this.canManage || !this.local.preview) {
            return false;
        }
        return this.save(false, this.local.preview.confirmation_token);
    }

    async save(preserve, confirmationToken = false) {
        if (
            !this.canManage ||
            (!preserve &&
                this.local.policy.enabled &&
                this.local.policy.preserve &&
                !confirmationToken)
        ) {
            return false;
        }
        const channelId = this.props.channelId;
        const sequence = ++this.sequence;
        this.local.phase = "saving";
        this.local.error = "";
        this.local.notice = "";
        try {
            const result = await this.props.store.setRetentionPreserve(
                channelId,
                preserve,
                confirmationToken
            );
            if (!this.current(sequence, channelId)) {
                return false;
            }
            this.local.policy = result.policy;
            this.local.preview = false;
            this.local.phase = "ready";
            this.local.notice = preserve
                ? "Histórico preservado. Conteúdo já excluído não é recuperado."
                : "Este grupo voltou a seguir a regra da caixa.";
            return true;
        } catch (_error) {
            if (this.current(sequence, channelId)) {
                this.local.phase = "error";
                this.local.preview = false;
                this.local.error =
                    "Não foi possível confirmar a alteração. Atualize a regra antes de tentar novamente.";
            }
            return false;
        }
    }
}

HistoryRetention.template = "contact_center_ui.HistoryRetention";
HistoryRetention.props = {
    store: Object,
    channelId: Number,
    policy: {type: [Object, Boolean], optional: true},
    visible: Boolean,
    focusRequest: {type: Number, optional: true},
};
