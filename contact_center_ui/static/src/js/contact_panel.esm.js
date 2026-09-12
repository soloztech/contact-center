/** @odoo-module **/

import {Component, useEffect, useRef, useState} from "@odoo/owl";

import {
    conversationAvatarUrl,
    conversationDisplayName,
    groupMetadataStateMeta,
    groupMetadataUi,
    groupRoleMeta,
    initials,
    isGroupConversation,
    partnerCompanyForIdentity,
    secondaryCompaniesForIdentity,
} from "./contact_center_model.esm";
import {AttributionTouchpoints} from "./attribution_touchpoints.esm";
import {ConfirmationDialog} from "@web/core/confirmation_dialog/confirmation_dialog";
import {DeferredImage} from "./deferred_image.esm";
import {HistoryRetention} from "./history_retention.esm";
import {deserializeDateTime} from "@web/core/l10n/dates";
import {useService} from "@web/core/utils/hooks";

export function companyRelationshipDetails(company, relationKind = "primary") {
    const lines = [company.name];
    for (const [field, label] of [
        ["vat", "CNPJ / VAT"],
        ["phone", "Telefone"],
        ["email", "E-mail"],
    ]) {
        if (company[field]) {
            lines.push(`${label}: ${company[field]}`);
        }
    }
    lines.push(
        relationKind === "secondary"
            ? "Vínculo secundário: não altera a empresa principal nem o cliente padrão de novas cotações."
            : "Vínculo principal: referência comercial e cliente padrão de novas cotações. O Odoo sincroniza os dados fiscais e de endereço."
    );
    return lines.join("\n");
}

export class CompanyRelationshipSummary extends Component {
    setup() {
        this.state = useState({tooltipDismissed: false});
    }

    get details() {
        return companyRelationshipDetails(this.props.company, this.props.relationKind);
    }

    get tooltipId() {
        return `cc-company-tooltip-${this.props.relationKind}-${this.props.company.id}`;
    }

    onKeydown(event) {
        if (event.key === "Escape") {
            this.state.tooltipDismissed = true;
            event.stopPropagation();
        }
    }
}

CompanyRelationshipSummary.template = "contact_center_ui.CompanyRelationshipSummary";
CompanyRelationshipSummary.props = {
    company: Object,
    relationKind: String,
    canManage: Boolean,
    mutationPending: Boolean,
    onOpen: Function,
    onUnlink: Function,
};

export function effectiveAgentsForConversation(conversation, teams, agents) {
    if (!Array.isArray(agents)) {
        return [];
    }
    const userIds = [];
    const accessUsers = conversation && conversation.access_users;
    if (Array.isArray(accessUsers)) {
        userIds.push(...accessUsers.filter((user) => user).map((user) => user.id));
    }
    const accessTeams = conversation && conversation.access_teams;
    const accessTeamIds = new Set(
        (Array.isArray(accessTeams) ? accessTeams : [])
            .filter((team) => team)
            .map((team) => team.id)
    );
    for (const team of Array.isArray(teams) ? teams : []) {
        if (!team || !accessTeamIds.has(team.id) || !Array.isArray(team.agent_ids)) {
            continue;
        }
        userIds.push(...team.agent_ids);
    }
    const allowed = new Set(
        userIds.filter((userId) => Number.isSafeInteger(userId) && userId > 0)
    );
    return agents.filter((agent) => agent && allowed.has(agent.id));
}

export class ContactPanel extends Component {
    setup() {
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.renameInputRef = useRef("renameInput");
        this.companyInputRef = useRef("companyInput");
        this.companyLinkTriggerRef = useRef("companyLinkTrigger");
        this.companyLinkerWasOpen = false;
        this.local = useState({
            name: "",
            phone: "",
            email: "",
            companyName: "",
            companyVat: "",
            companyPhone: "",
            companyEmail: "",
            renameOpen: false,
            renameName: "",
            renamePhase: "idle",
        });
        useEffect(
            () => this.resetPanelForms(),
            () => [this.props.state.selectedChannelId]
        );
        useEffect(
            () => {
                const channelId = this.props.state.selectedChannelId;
                const attribution = this.props.state.attribution;
                if (!this.canViewAttribution) {
                    if (
                        attribution &&
                        (attribution.phase !== "idle" ||
                            attribution.enabled ||
                            attribution.items.length)
                    ) {
                        this.props.store.resetAttribution(channelId || false);
                    }
                    return;
                }
                if (
                    this.props.state.detailsOpen &&
                    channelId &&
                    (!attribution ||
                        attribution.channelId !== channelId ||
                        attribution.phase === "idle")
                ) {
                    this.props.store.loadAttribution({silent: true});
                }
            },
            () => [
                this.props.state.detailsOpen,
                this.props.state.selectedChannelId,
                this.props.conversation &&
                    this.props.conversation.capabilities &&
                    this.props.conversation.capabilities.view_attribution,
                this.props.state.attribution && this.props.state.attribution.channelId,
                this.props.state.attribution && this.props.state.attribution.phase,
            ]
        );
        useEffect(
            () => {
                if (this.local.renameOpen && this.renameInputRef.el) {
                    this.renameInputRef.el.focus();
                    this.renameInputRef.el.select();
                }
            },
            () => [this.local.renameOpen]
        );
        useEffect(
            () => {
                const isOpen = this.state.companyLinker.open;
                if (isOpen && this.companyInputRef.el) {
                    this.companyInputRef.el.focus();
                } else if (this.companyLinkerWasOpen && this.companyLinkTriggerRef.el) {
                    this.companyLinkTriggerRef.el.focus();
                }
                this.companyLinkerWasOpen = isOpen;
            },
            () => [this.state.companyLinker.open, this.state.companyLinker.mode]
        );
    }

    get store() {
        return this.props.store;
    }

    get state() {
        return this.props.state;
    }

    get conversation() {
        return this.props.conversation;
    }

    get canViewAttribution() {
        return this.store.canViewAttribution(this.conversation);
    }

    get identity() {
        return (this.conversation && this.conversation.identity) || false;
    }

    get company() {
        return partnerCompanyForIdentity(this.identity);
    }

    get partner() {
        return (this.identity && this.identity.partner) || false;
    }

    get secondaryCompanies() {
        return secondaryCompaniesForIdentity(this.identity);
    }

    get hasCompanyRelationships() {
        return Boolean(this.company || this.secondaryCompanies.length);
    }

    get canManageCompany() {
        return Boolean(
            this.partner &&
                this.partner.company_management_allowed &&
                this.store.capabilities.link_company
        );
    }

    get isLinkedCompany() {
        return Boolean(this.partner && this.identity.link_kind === "central_company");
    }

    get linkInvariantValid() {
        return !this.partner || this.identity.link_invariant_valid !== false;
    }

    get identityKicker() {
        if (this.isGroup) {
            return "Grupo";
        }
        if (!this.identity) {
            return "Conversa externa";
        }
        if (this.isLinkedCompany) {
            return "Empresa vinculada";
        }
        return this.identity.persona_kind === "contact"
            ? "Contato vinculado"
            : "Perfil convidado";
    }

    get centralCompanyLinker() {
        return this.state.contactLinker.targetKind === "central_company";
    }

    get canLinkCentralCompany() {
        return Boolean(this.store.capabilities.link_central_company);
    }

    get canCreateCentralCompany() {
        return Boolean(this.store.capabilities.create_central_company);
    }

    get canManageCentralCompany() {
        return this.canLinkCentralCompany || this.canCreateCentralCompany;
    }

    get canUnlinkIdentity() {
        return Boolean(
            this.partner &&
                (this.isLinkedCompany
                    ? this.canLinkCentralCompany
                    : !this.hasCompanyRelationships)
        );
    }

    get companyProjectionUnavailable() {
        const partner = this.identity && this.identity.partner;
        return Boolean(partner && partner.company && !this.company);
    }

    get canLinkCompany() {
        return Boolean(
            this.identity &&
                this.identity.partner &&
                this.identity.link_kind === "person" &&
                this.identity.partner.is_company === false &&
                ((this.identity.partner.company === false &&
                    this.identity.partner.company_linking_allowed === true) ||
                    (this.company &&
                        this.identity.partner.secondary_company_linking_allowed ===
                            true)) &&
                this.store.capabilities.link_company === true
        );
    }

    get canCreateCompany() {
        return Boolean(
            this.canLinkCompany && this.store.capabilities.create_company === true
        );
    }

    get companyMutationPending() {
        const partner = this.identity && this.identity.partner;
        return Boolean(
            partner &&
                this.store.companyOperationPending(
                    this.state.selectedChannelId,
                    partner.id
                )
        );
    }

    get companyRelationState() {
        if (this.company) {
            return "linked";
        }
        return this.state.companyLinker.open ? "editing" : "empty";
    }

    get canRenameGuest() {
        return Boolean(
            this.identity &&
                this.identity.persona_kind === "guest" &&
                this.store.capabilities.rename_guest
        );
    }

    get isGroup() {
        return isGroupConversation(this.conversation);
    }

    get displayName() {
        return conversationDisplayName(this.conversation);
    }

    get avatarUrl() {
        return conversationAvatarUrl(this.conversation);
    }

    get groupMetadata() {
        return groupMetadataUi(this.conversation);
    }

    get groupState() {
        return groupMetadataStateMeta(
            this.groupMetadata ? this.groupMetadata.metadata_state : "unavailable"
        );
    }

    get groupRole() {
        return groupRoleMeta(
            this.groupMetadata ? this.groupMetadata.own_role : "unknown"
        );
    }

    get groupLastSyncLabel() {
        const value = this.groupMetadata && this.groupMetadata.last_synced_at;
        if (!value) {
            return "";
        }
        try {
            const date = deserializeDateTime(value);
            return date.isValid ? date.toFormat("dd/LL/yyyy 'às' HH:mm") : "";
        } catch (_error) {
            return "";
        }
    }

    initials(name) {
        return initials(name);
    }

    isTagSelected(tagId) {
        return Boolean(
            this.conversation &&
                (this.conversation.tags || []).some((tag) => tag.id === tagId)
        );
    }

    get effectiveAgents() {
        return effectiveAgentsForConversation(
            this.conversation,
            this.store.teams,
            this.store.agents
        );
    }

    resetContactForm() {
        const identity = this.identity;
        this.local.name = (identity && identity.name) || "";
        this.local.phone = (identity && identity.suggested_phone) || "";
        this.local.email = "";
    }

    resetCompanyForm() {
        this.local.companyName = "";
        this.local.companyVat = "";
        this.local.companyPhone = "";
        this.local.companyEmail = "";
    }

    resetCentralCompanyForm() {
        this.resetCompanyForm();
        this.local.companyPhone =
            (this.identity && this.identity.suggested_phone) || "";
    }

    resetPanelForms() {
        this.resetContactForm();
        this.resetCompanyForm();
        this.cancelGuestRename();
    }

    openGuestRename() {
        if (!this.canRenameGuest) {
            return;
        }
        this.local.renameName = this.identity.name || "";
        this.local.renamePhase = "idle";
        this.local.renameOpen = true;
    }

    cancelGuestRename() {
        this.local.renameOpen = false;
        this.local.renameName = "";
        this.local.renamePhase = "idle";
    }

    onGuestRenameKeydown(event) {
        if (event.key === "Escape") {
            event.preventDefault();
            this.cancelGuestRename();
        }
    }

    async renameGuest() {
        const name = this.local.renameName.trim();
        if (!name || this.local.renamePhase === "saving") {
            return;
        }
        this.local.renamePhase = "saving";
        const renamed = await this.store.renameGuest(name);
        if (renamed) {
            this.cancelGuestRename();
        } else {
            this.local.renamePhase = "error";
        }
    }

    onResponsibleChange(event) {
        this.store.setResponsible(event.target.value);
    }

    onContactSearch(event) {
        this.store.setContactSearch(event.target.value);
    }

    onCompanySearch(event) {
        this.store.setCompanySearch(event.target.value);
    }

    onCompanyLinkerKeydown(event) {
        if (event.key === "Escape" && this.state.companyLinker.phase !== "saving") {
            event.preventDefault();
            this.closeCompanyLinker();
        }
    }

    closeCompanyLinker() {
        this.store.closeCompanyLinker();
    }

    openCreateContact() {
        if (this.store.openContactLinker("create", "person")) {
            this.resetContactForm();
        }
    }

    openPersonLinker() {
        this.store.openContactLinker("search", "person");
    }

    openCentralCompanyLinker() {
        const mode = this.canLinkCentralCompany ? "search" : "create";
        if (
            this.store.openContactLinker(mode, "central_company") &&
            mode === "create"
        ) {
            this.resetCentralCompanyForm();
        }
    }

    openCreateCentralCompany() {
        if (this.store.openContactLinker("create", "central_company")) {
            this.resetCentralCompanyForm();
        }
    }

    async createContact() {
        if (this.state.contactLinker.phase === "saving") {
            return;
        }
        await this.store.createAndLinkPartner({
            name: this.local.name,
            phone: this.local.phone,
            email: this.local.email,
        });
    }

    async createCentralCompany() {
        if (this.state.contactLinker.phase === "saving") {
            return;
        }
        await this.store.createAndLinkCentralCompany({
            name: this.local.companyName,
            vat: this.local.companyVat,
            phone: this.local.companyPhone,
            email: this.local.companyEmail,
        });
    }

    async openPartnerRecord(partnerId) {
        const candidates = [
            this.partner,
            this.company,
            ...(this.secondaryCompanies || []),
        ].filter(Boolean);
        const current = candidates.find(
            (candidate) =>
                Number.isSafeInteger(candidate.id) &&
                candidate.id > 0 &&
                candidate.id === partnerId
        );
        if (!current) {
            return false;
        }
        try {
            await this.action.doAction({
                type: "ir.actions.act_window",
                name: current.name || "Contato",
                res_model: "res.partner",
                res_id: current.id,
                views: [[false, "form"]],
                view_mode: "form",
                target: "new",
            });
            return true;
        } catch (_error) {
            this.store.notify("Não foi possível abrir este cadastro.", {
                type: "danger",
                title: "Cadastro indisponível",
            });
            return false;
        }
    }

    openCreateCompany() {
        const shouldReset = !this.state.companyLinker.open;
        if (this.store.openCompanyLinker("create") && shouldReset) {
            this.resetCompanyForm();
        }
    }

    unlinkCompany(company, relationKind = "primary") {
        this.dialog.add(ConfirmationDialog, {
            title: "Desvincular empresa",
            body: `Remover o vínculo de ${this.partner.name} com ${company.name}? O contato será mantido. Esta alteração vale para o cadastro do contato em todas as conversas; documentos existentes serão preservados.`,
            confirmLabel: "Desvincular empresa",
            confirm: () => this.store.unlinkPartnerCompany(company.id, relationKind),
        });
    }

    correctLinkedContact() {
        this.dialog.add(ConfirmationDialog, {
            title: "Corrigir contato vinculado",
            body: "Desvincular este perfil do contato? O cadastro da pessoa e seus vínculos com empresas serão preservados. Depois, você poderá selecionar a pessoa correta.",
            confirmLabel: "Desvincular pessoa",
            confirm: () => this.store.unlinkPartner(),
        });
    }

    async createCompany() {
        if (this.state.companyLinker.phase === "saving") {
            return;
        }
        await this.store.createAndLinkPartnerCompany({
            name: this.local.companyName,
            vat: this.local.companyVat,
            phone: this.local.companyPhone,
            email: this.local.companyEmail,
        });
    }
}

ContactPanel.components = {
    AttributionTouchpoints,
    CompanyRelationshipSummary,
    DeferredImage,
    HistoryRetention,
};
ContactPanel.props = {conversation: Object, state: Object, store: Object};
ContactPanel.template = "contact_center_ui.ContactPanel";
