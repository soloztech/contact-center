/** @odoo-module **/
import {Component, onWillDestroy, onWillStart, useState} from "@odoo/owl";
import {deserializeDateTime, formatDateTime} from "@web/core/l10n/dates";
import {Dialog} from "@web/core/dialog/dialog";
import {openContactCenterChat} from "@contact_center_ui/js/contact_center_messaging.esm";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";
import {validateEnvelope} from "@contact_center_ui/js/contact_center_model.esm";

const blank = () => ({
    items: [],
    total: 0,
    offset: 0,
    limit: 20,
    has_more: false,
    status: "ready",
    loading: false,
    error: "",
});
export class JourneyDialog extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.alive = true;
        this.state = useState({
            linked: blank(),
            context: blank(),
            histories: {},
            opening: false,
            error: "",
        });
        onWillDestroy(() => {
            this.alive = false;
        });
        onWillStart(() => {
            this.load("linked", 0);
            this.load("context", 0);
        });
    }
    isAlive() {
        return this.alive && !this.props.closeState.closed;
    }
    date(value, fallback = "Não identificado") {
        return value ? formatDateTime(deserializeDateTime(value)) : fallback;
    }
    visibleOrigins(row, area) {
        return row.origins.items.filter(
            (origin) => area === "linked" || origin.type !== "website_whatsapp"
        );
    }
    provenance(value) {
        return (
            {
                referrer: "URL da página clicada",
                track: "Histórico de visitas do site",
                cookie: "Cookie de campanha",
                none: "Origem não identificada",
                legacy: "Captura anterior",
            }[value] || "Origem não identificada"
        );
    }
    conversationState(value) {
        return (
            {open: "Aberta", resolved: "Resolvida", archived: "Arquivada"}[value] ||
            value
        );
    }
    event(value) {
        return (
            {
                baseline: "Início da cobertura histórica",
                created: "Conversa criada",
                assigned: "Responsável atribuído",
                unassigned: "Responsável removido",
                resolved: "Conversa resolvida",
                reopened: "Conversa reaberta",
                archived: "Conversa arquivada",
                unarchived: "Conversa desarquivada",
            }[value] || "Evento de atendimento"
        );
    }
    origin(value) {
        return (
            {
                conversation_start: "Início de conversa",
                entry_point: "Entrada no site",
                form_submission: "Formulário enviado",
                lead_ad: "Cadastro por anúncio",
                organic_link: "Link orgânico",
                paid_ad_click: "Clique em anúncio",
                paid_ad_signal: "Sinal de anúncio",
                unknown: "Origem não identificada",
            }[value] || "Origem de aquisição"
        );
    }
    websiteStatus(value) {
        return (
            {
                eligible: "Origem dentro do período confirmado",
                outside_period: "Origem fora do período deste negócio",
                scope_review: "Contexto: revisar período para atribuir crédito",
                pending: "Aguardando processamento",
                legacy_collision: "Evento já capturado anteriormente: revisar origem",
                legacy: "Captura anterior à jornada: sem crédito automático",
                capture_unavailable: "Captura bloqueada pela configuração atual",
                decision_changed:
                    "Decisão de consentimento alterada antes da atribuição",
                privacy_unavailable: "Origem indisponível por privacidade",
                claim_unavailable: "Associação indisponível para atribuição",
                business_context: "Negócio de contexto: empresa ainda não atribuída",
                support_review: "Sem atribuição ativa: revisar origem",
                suggested: "Sugestão sem crédito",
                rejected: "Associação descartada",
                superseded: "Associação superada",
                reference: "Código recebido",
                confirmed: "Confirmado pela equipe",
            }[value] || "Sem negócio atribuído"
        );
    }
    websiteAssociation(value) {
        return (
            {
                reference: "Código recebido na mensagem",
                confirmed: "Associação confirmada pela equipe",
                suggested: "Sugestão: sem confirmação e sem crédito",
                rejected: "Associação descartada",
                superseded: "Associação superada",
            }[value] || "Associação não identificada"
        );
    }
    websiteCampaign(value) {
        if (value.status === "restricted") return "Campanha com acesso restrito";
        if (value.status === "utm") return `UTM informado: ${value.name || ""}`;
        if (value.status === "ambiguous")
            return `Campanha na URL: ${value.url_campaign_id} (mais de uma conta no catálogo)`;
        if (value.status === "resolved")
            return value.name || `Campanha na URL: ${value.url_campaign_id}`;
        return value.url_campaign_id
            ? `Campanha na URL: ${value.url_campaign_id} (não encontrada no catálogo)`
            : "Campanha não identificada";
    }
    async evidence(origin) {
        if (!this.isAlive() || this.state.opening) {
            return;
        }
        this.state.opening = true;
        this.state.error = "";
        try {
            const action = await this.orm.call(
                "crm.lead",
                "action_website_journey_match",
                [[this.props.leadId], origin.match_id]
            );
            if (this.isAlive()) {
                await this.action.doAction(action);
            }
        } catch {
            if (this.isAlive()) {
                this.state.error = "Origem indisponível. Confira seu acesso.";
            }
        } finally {
            if (this.isAlive()) {
                this.state.opening = false;
            }
        }
    }
    async moreOrigins(row) {
        if (!this.isAlive() || this.state.opening) {
            return;
        }
        this.state.opening = true;
        this.state.error = "";
        try {
            const action = await this.orm.call(
                "crm.lead",
                "action_website_journey_matches",
                [[this.props.leadId], row.channel_id]
            );
            if (this.isAlive()) {
                await this.action.doAction(action);
            }
        } catch {
            if (this.isAlive()) {
                this.state.error = "Origens indisponíveis. Confira seu acesso.";
            }
        } finally {
            if (this.isAlive()) {
                this.state.opening = false;
            }
        }
    }
    scope(value) {
        return (
            {
                legacy: "Legado: revisar período",
                context: "Contexto sem crédito",
                confirmed: "Período confirmado",
                review: "Revisar período após fusão",
                customer_context: "Contexto do cliente",
            }[value] || value
        );
    }
    async load(area, offset = 0) {
        const page = this.state[area];
        if (!this.isAlive() || page.loading || this.state.opening) {
            return;
        }
        Object.assign(page, {loading: true, error: "", items: [], offset});
        try {
            const result = await this.orm.call(
                "crm.lead",
                "get_contact_center_journey",
                [[this.props.leadId]],
                {area, offset, limit: 20}
            );
            if (this.isAlive()) {
                Object.assign(page, result);
                this.state.error = "";
            }
        } catch {
            if (this.isAlive()) {
                page.has_more = false;
                page.error = "Não foi possível carregar a jornada. Tente novamente.";
            }
        } finally {
            if (this.isAlive()) {
                page.loading = false;
            }
        }
    }
    async open(row) {
        if (!this.isAlive() || this.state.opening || !row.can_open) {
            return;
        }
        this.state.opening = true;
        this.state.error = "";
        try {
            const messaging = await this.env.services.messaging.get();
            if (!this.isAlive()) {
                return;
            }
            const result = await this.orm.call(
                "crm.lead",
                "open_contact_center_journey_conversation",
                [[this.props.leadId], row.channel_id]
            );
            if (!this.isAlive()) {
                return;
            }
            validateEnvelope(result);
            openContactCenterChat(messaging, result.item);
            this.props.close();
        } catch {
            if (this.isAlive()) {
                this.state.opening = false;
                await Promise.all([this.load("linked"), this.load("context")]);
                if (this.isAlive()) {
                    this.state.error =
                        "Não foi possível abrir a conversa. Confira seu acesso.";
                }
            }
        } finally {
            if (this.isAlive()) {
                this.state.opening = false;
            }
        }
    }
    async confirm(row) {
        if (!this.isAlive() || this.state.opening || !row.can_open) {
            return;
        }
        this.state.opening = true;
        this.state.error = "";
        try {
            const action = await this.orm.call(
                "crm.lead",
                "action_contact_center_scope",
                [[this.props.leadId], row.channel_id]
            );
            if (this.isAlive()) {
                await this.action.doAction(action, {
                    onClose: () => this.isAlive() && this.load("linked"),
                });
            }
        } catch {
            if (this.isAlive()) {
                this.state.error = "Não foi possível revisar o período.";
            }
        } finally {
            if (this.isAlive()) {
                this.state.opening = false;
            }
        }
    }
    async history(row, offset = 0) {
        if (!this.isAlive() || this.state.opening || !row.can_open) {
            return;
        }
        const key = row.channel_id;
        if (!this.state.histories[key]) {
            this.state.histories[key] = {...blank(), visible: true};
        }
        const page = this.state.histories[key];
        if (page.loading) {
            return;
        }
        page.loading = true;
        try {
            const result = await this.orm.call(
                "crm.lead",
                "get_contact_center_journey_history",
                [[this.props.leadId], key],
                {offset, limit: 20}
            );
            if (this.isAlive()) {
                Object.assign(page, result, {error: ""});
            }
        } catch {
            if (this.isAlive()) {
                page.error = "Histórico indisponível. Tente novamente.";
            }
        } finally {
            if (this.isAlive()) {
                page.loading = false;
            }
        }
    }
}
JourneyDialog.template = "contact_center_crm.JourneyDialog";
JourneyDialog.components = {Dialog};
JourneyDialog.props = {leadId: Number, closeState: Object, close: Function};
export function showJourney(env, action) {
    const closeState = {closed: false};
    env.services.dialog.add(
        JourneyDialog,
        {leadId: action.params.lead_id, closeState},
        {
            onClose: () => {
                closeState.closed = true;
            },
        }
    );
}
registry.category("actions").add("contact_center_crm.journey", showJourney);
