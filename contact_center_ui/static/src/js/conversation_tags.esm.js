/** @odoo-module **/

import {Component, onWillDestroy, useEffect, useRef, useState} from "@odoo/owl";

export function conversationTagSelected(conversation, tagId) {
    return Boolean(
        conversation && (conversation.tags || []).some((tag) => tag.id === tagId)
    );
}

export class ConversationTags extends Component {
    setup() {
        this.rootRef = useRef("root");
        this.triggerRef = useRef("trigger");
        this.searchRef = useRef("search");
        this.request = 0;
        this.local = useState({
            open: false,
            loading: false,
            saving: false,
            items: [],
            query: "",
            canCreate: false,
            error: "",
            notice: "",
        });
        useEffect(
            () => {
                this.close(false);
            },
            () => [this.props.conversation.channel_id]
        );
        useEffect(
            () => {
                if (!this.local.open) {
                    return;
                }
                if (this.searchRef.el) {
                    this.searchRef.el.focus();
                }
                const onPointer = (event) => {
                    if (this.rootRef.el && !this.rootRef.el.contains(event.target)) {
                        this.close(false);
                    }
                };
                document.addEventListener("pointerdown", onPointer);
                return () => document.removeEventListener("pointerdown", onPointer);
            },
            () => [this.local.open]
        );
        onWillDestroy(() => {
            this.request++;
        });
    }

    get selectedCount() {
        return (this.props.conversation.tags || []).length;
    }

    get visibleTags() {
        const query = this.local.query.trim().toLocaleLowerCase();
        return this.local.items.filter((tag) =>
            tag.name.toLocaleLowerCase().includes(query)
        );
    }

    get canCreateName() {
        const name = this.local.query.trim();
        return Boolean(
            this.local.canCreate &&
                name &&
                !this.local.items.some(
                    (tag) => tag.name.toLocaleLowerCase() === name.toLocaleLowerCase()
                )
        );
    }

    isSelected(tagId) {
        return conversationTagSelected(this.props.conversation, tagId);
    }

    close(restoreFocus = true) {
        this.request++;
        this.local.open = false;
        this.local.loading = false;
        this.local.saving = false;
        if (restoreFocus && this.triggerRef.el) {
            this.triggerRef.el.focus();
        }
    }

    onKeydown(event) {
        if (event.key === "Escape" && this.local.open) {
            event.preventDefault();
            event.stopPropagation();
            this.close();
        }
    }

    async toggle() {
        if (this.local.open) {
            this.close();
            return;
        }
        this.local.open = true;
        this.local.query = "";
        this.local.error = "";
        this.local.notice = "";
        this.local.loading = true;
        this.local.canCreate = false;
        this.local.items = [];
        const request = ++this.request;
        const channelId = this.props.conversation.channel_id;
        try {
            const payload = await this.props.store.call("conversation_tag_catalog", [
                channelId,
            ]);
            if (request !== this.request) {
                return;
            }
            this.local.items = payload.items;
            this.local.canCreate = payload.can_create === true;
            this.props.store.reconcileTagCatalog(payload.items);
        } catch (_error) {
            if (request === this.request) {
                this.local.error = "Não foi possível carregar os marcadores.";
            }
        } finally {
            if (request === this.request) {
                this.local.loading = false;
            }
        }
    }

    async toggleTag(tagId) {
        if (this.local.saving) {
            return;
        }
        const request = this.request;
        this.local.saving = true;
        this.local.error = "";
        this.local.notice = "";
        try {
            await this.props.store.toggleTag(tagId);
        } finally {
            if (request === this.request) {
                this.local.saving = false;
            }
        }
    }

    async createTag(event) {
        event.preventDefault();
        if (!this.canCreateName || this.local.saving || this.local.loading) {
            return;
        }
        const channelId = this.props.conversation.channel_id;
        const request = this.request;
        this.local.saving = true;
        this.local.error = "";
        this.local.notice = "";
        try {
            const payload = await this.props.store.call("create_conversation_tag", [
                channelId,
                this.local.query.trim(),
            ]);
            this.props.store.reconcileTagCatalog(payload.items);
            if (request !== this.request) {
                return;
            }
            this.local.items = payload.items;
            this.local.query = "";
            this.local.notice =
                "Marcador cadastrado. Selecione-o para aplicar à conversa.";
        } catch (_error) {
            if (request === this.request) {
                this.local.error =
                    "Não foi possível criar o marcador. Verifique suas permissões e tente novamente.";
            }
        } finally {
            if (request === this.request) {
                this.local.saving = false;
            }
        }
    }
}

ConversationTags.template = "contact_center_ui.ConversationTags";
ConversationTags.props = {store: Object, conversation: Object};
