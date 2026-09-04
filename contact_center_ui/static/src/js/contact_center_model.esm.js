/** @odoo-module **/

import {_t} from "@web/core/l10n/translation";

/**
 * Pure data helpers shared by the Contact Center client action and its tests.
 *
 * This module deliberately has no Owl, service, browser, or DOM dependency. UI
 * components remain responsible for escaping strings when rendering them.
 */

export const SUPPORTED_SCHEMA_VERSION = 1;
export const CONTACT_CENTER_NOTIFICATION_TYPE = "contact_center/event";
export const CONNECTION_HEALTH_EVENT_TYPE = "connection_health_updated";

export function isGroupConversation(conversation) {
    return Boolean(
        conversation &&
            typeof conversation === "object" &&
            conversation.conversation_type === "group"
    );
}

const DANGEROUS_PROPERTY_NAMES = new Set(["__proto__", "constructor", "prototype"]);
const EVENT_TYPE_PATTERN = /^[a-z][a-z0-9_.-]*$/;
const UUID_PATTERN =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const WORD_PATTERN = /[\p{L}\p{N}]+/gu;
const MEDIA_KINDS = Object.freeze(["image", "audio", "video", "document"]);
const MEDIA_STATES = new Set(["pending", "ready", "failed"]);
const GROUP_METADATA_FIELDS = Object.freeze([
    "display_name",
    "avatar_url",
    "participant_count",
    "admin_count",
    "own_role",
    "metadata_state",
    "last_synced_at",
]);
const GROUP_ROLES = new Set(["unknown", "member", "admin", "superadmin"]);
const CONNECTION_ROLES = new Set(["primary", "standby", "migration", "historical"]);
const GROUP_METADATA_STATES = new Set([
    "unavailable",
    "pending",
    "ready",
    "stale",
    "failed",
]);
const ATTRIBUTION_TOUCHPOINT_TYPES = new Set([
    "paid_ad_click",
    "paid_ad_signal",
    "entry_point",
    "organic_link",
    "unknown",
]);
const ATTRIBUTION_EVIDENCE_LEVELS = new Set([
    "provider_asserted",
    "provider_asserted_non_paid",
    "provider_hint",
    "observed",
    "derived",
]);
const ATTRIBUTION_KEY_PATTERN = /^[a-z][a-z0-9_.-]{0,127}$/;
const ODOO_DATETIME_PATTERN =
    /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?$/;
const DEFAULT_MAX_MEDIA_BYTES = Object.freeze({
    image: 16 * 1024 * 1024,
    audio: 16 * 1024 * 1024,
    video: 50 * 1024 * 1024,
    document: 50 * 1024 * 1024,
});
const MESSAGE_ACTION_POLICY_KEYS = Object.freeze({
    reply: "allow_reply",
    react: "allow_react",
    edit: "allow_edit",
    delete: "allow_delete",
    resend: "allow_send",
});

const DELIVERY_METADATA = Object.freeze({
    queued: Object.freeze({
        state: "queued",
        rank: 0,
        label: "Na fila",
        tone: "pending",
        icon: "○",
    }),
    failed: Object.freeze({
        state: "failed",
        rank: 1,
        label: "Falhou",
        tone: "danger",
        icon: "!",
    }),
    sent: Object.freeze({
        state: "sent",
        rank: 2,
        label: "Enviada",
        tone: "progress",
        icon: "✓",
    }),
    delivered: Object.freeze({
        state: "delivered",
        rank: 3,
        label: "Entregue",
        tone: "success",
        icon: "✓✓",
    }),
    read: Object.freeze({
        state: "read",
        rank: 4,
        label: "Lida",
        tone: "success",
        icon: "✓✓",
    }),
});
const EMPTY_DELIVERY_METADATA = Object.freeze({
    state: "",
    rank: -1,
    label: "",
    tone: "muted",
    icon: "",
});
const UNKNOWN_DELIVERY_METADATA = Object.freeze({
    state: "unknown",
    rank: -1,
    label: "Status desconhecido",
    tone: "warning",
    icon: "○",
});
const DISPATCH_METADATA = Object.freeze({
    cancelled: Object.freeze({
        state: "cancelled",
        rank: -1,
        label: "Envio cancelado",
        tone: "muted",
        icon: "×",
    }),
    dead: Object.freeze({
        state: "dead",
        rank: -1,
        label: "Falha permanente",
        tone: "danger",
        icon: "!",
    }),
    processing: Object.freeze({
        state: "processing",
        rank: 0,
        label: "Enviando",
        tone: "progress",
        icon: "○",
    }),
    retry: Object.freeze({
        state: "retry",
        rank: 0,
        label: "Tentando novamente",
        tone: "warning",
        icon: "○",
    }),
    uncertain: Object.freeze({
        state: "uncertain",
        rank: -1,
        label: "Envio incerto — não reenviar",
        tone: "warning",
        icon: "?",
    }),
});
const DISPATCH_REASON_METADATA = Object.freeze({
    waiting_queue: Object.freeze({
        code: "waiting_queue",
        label: _t("Aguardando processamento da fila."),
        tone: "muted",
    }),
    waiting_connection: Object.freeze({
        code: "waiting_connection",
        label: _t("Aguardando uma conexão saudável para enviar."),
        tone: "warning",
    }),
    waiting_provider_limit: Object.freeze({
        code: "waiting_provider_limit",
        label: _t("Aguardando o limite de envio do provedor."),
        tone: "warning",
    }),
    sending: Object.freeze({
        code: "sending",
        label: _t("Envio em andamento."),
        tone: "progress",
    }),
    automatic_retry: Object.freeze({
        code: "automatic_retry",
        label: _t("Nova tentativa automática agendada."),
        tone: "warning",
    }),
    permanent_failure: Object.freeze({
        code: "permanent_failure",
        label: _t("O envio falhou de forma permanente."),
        tone: "danger",
    }),
    uncertain: Object.freeze({
        code: "uncertain",
        label: _t("O provedor pode ter recebido a mensagem; não reenvie."),
        tone: "warning",
    }),
    cancelled: Object.freeze({
        code: "cancelled",
        label: _t("O envio foi encerrado."),
        tone: "muted",
    }),
    retry_created: Object.freeze({
        code: "retry_created",
        label: _t("Uma nova tentativa já foi criada para esta mensagem."),
        tone: "muted",
    }),
    retry_capability_unavailable: Object.freeze({
        code: "retry_capability_unavailable",
        label: _t("O canal não permite reenviar este conteúdo agora."),
        tone: "warning",
    }),
    retry_connection_unavailable: Object.freeze({
        code: "retry_connection_unavailable",
        label: _t("A conexão precisa estar saudável para reenviar."),
        tone: "warning",
    }),
    retry_content_unavailable: Object.freeze({
        code: "retry_content_unavailable",
        label: _t("O conteúdo original não está seguro para reenvio."),
        tone: "danger",
    }),
    retry_reply_unavailable: Object.freeze({
        code: "retry_reply_unavailable",
        label: _t("A mensagem respondida não está disponível para reenvio."),
        tone: "danger",
    }),
    retry_media_unavailable: Object.freeze({
        code: "retry_media_unavailable",
        label: _t("A mídia original não está disponível para reenvio."),
        tone: "danger",
    }),
});
const DISPATCH_REASON_CODES = new Set(Object.keys(DISPATCH_REASON_METADATA));
const EMPTY_DISPATCH_REASON_METADATA = Object.freeze({
    code: "",
    label: "",
    tone: "muted",
});
const REALTIME_STATUS_METADATA = Object.freeze({
    connecting: Object.freeze({
        state: "connecting",
        label: "Conectando…",
        description: "Conectando às atualizações em tempo real",
    }),
    offline: Object.freeze({
        state: "offline",
        label: "Sem tempo real",
        description: "Atualizações em tempo real indisponíveis",
    }),
    online: Object.freeze({
        state: "online",
        label: "Tempo real ativo",
        description: "Atualizações em tempo real ativas",
    }),
});
const CONVERSATION_STATE_METADATA = Object.freeze({
    open: Object.freeze({key: "open", label: "Aberta", tone: "progress"}),
    resolved: Object.freeze({key: "resolved", label: "Resolvida", tone: "success"}),
});
const CONVERSATION_RESOLUTION_ACTIONS = Object.freeze({
    open: Object.freeze({
        target: "resolved",
        label: "Resolver",
        icon: "fa-check",
        tone: "resolve",
    }),
    resolved: Object.freeze({
        target: "open",
        label: "Reabrir",
        icon: "fa-undo",
        tone: "reopen",
    }),
});
const CONNECTION_HEALTH_METADATA = Object.freeze({
    connected: Object.freeze({
        state: "connected",
        label: "Conectada",
        description: "A conexão está disponível para atendimento.",
        tone: "success",
        priority: 5,
    }),
    degraded: Object.freeze({
        state: "degraded",
        label: "Atenção",
        description: "A conexão respondeu, mas apresenta instabilidade.",
        tone: "warning",
        priority: 2,
    }),
    disconnected: Object.freeze({
        state: "disconnected",
        label: "Desconectada",
        description: "A conexão está temporariamente indisponível.",
        tone: "danger",
        priority: 1,
    }),
    authentication_required: Object.freeze({
        state: "authentication_required",
        label: "Login necessário",
        description: "A sessão precisa ser autenticada novamente.",
        tone: "danger",
        priority: 0,
    }),
    checking: Object.freeze({
        state: "checking",
        label: "Verificando…",
        description: "Uma verificação de saúde está em andamento.",
        tone: "progress",
        priority: 3,
    }),
    unknown: Object.freeze({
        state: "unknown",
        label: "Sem diagnóstico",
        description: "Ainda não há uma verificação recente desta conexão.",
        tone: "muted",
        priority: 4,
    }),
});
const CONNECTION_HEALTH_STATES = Object.freeze([
    "connected",
    "degraded",
    "disconnected",
    "authentication_required",
    "unknown",
]);
const CONNECTION_HEALTH_ALERT_ORDER = Object.freeze([
    "authentication_required",
    "disconnected",
    "degraded",
    "checking",
    "unknown",
]);
const CONNECTION_HEALTH_DETAIL_LABELS = Object.freeze({
    healthy: "Conexão operacional.",
    metadata_limited:
        "Mensageria operacional; metadados do ativo não puderam ser validados.",
    authentication_required: "Faça login novamente para restabelecer a sessão.",
    session_not_authenticated: "Faça login novamente para restabelecer a sessão.",
    unavailable: "O provedor não respondeu à última verificação.",
    unreachable: "O provedor não pôde ser alcançado.",
    timeout: "A verificação excedeu o tempo de resposta.",
    network_error: "A rede não alcançou o provedor.",
    rate_limited: "O provedor limitou temporariamente as verificações.",
    provider_paused: "A conexão está pausada no provedor.",
    identity_mismatch: "O ativo conectado não corresponde ao configurado.",
    identity_unverified: "Não foi possível confirmar o ativo conectado.",
    invalid_response: "O provedor retornou uma resposta inválida.",
    provider_error: "O provedor reportou uma falha na conexão.",
    internal_error: "A verificação não pôde ser concluída.",
});
const GROUP_METADATA_STATE_METADATA = Object.freeze({
    unavailable: Object.freeze({
        state: "unavailable",
        label: "Sem dados",
        description: "Os detalhes do grupo ainda não estão disponíveis.",
        tone: "muted",
    }),
    pending: Object.freeze({
        state: "pending",
        label: "Sincronizando",
        description: "Os detalhes do grupo estão sendo sincronizados.",
        tone: "progress",
    }),
    ready: Object.freeze({
        state: "ready",
        label: "Sincronizado",
        description: "Os detalhes do grupo estão atualizados.",
        tone: "success",
    }),
    stale: Object.freeze({
        state: "stale",
        label: "Dados antigos",
        description: "Os detalhes do grupo aguardam uma nova sincronização.",
        tone: "warning",
    }),
    failed: Object.freeze({
        state: "failed",
        label: "Falha na sincronização",
        description: "A última sincronização dos detalhes do grupo falhou.",
        tone: "danger",
    }),
});
const GROUP_ROLE_METADATA = Object.freeze({
    unknown: Object.freeze({role: "unknown", label: "Não informado"}),
    member: Object.freeze({role: "member", label: "Membro"}),
    admin: Object.freeze({role: "admin", label: "Administrador"}),
    superadmin: Object.freeze({role: "superadmin", label: "Superadministrador"}),
});

function isPlainObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        return false;
    }
    try {
        const prototype = Object.getPrototypeOf(value);
        return prototype === Object.prototype || prototype === null;
    } catch {
        return false;
    }
}

function positiveInteger(value) {
    if (typeof value === "number") {
        return Number.isSafeInteger(value) && value > 0 ? value : null;
    }
    if (
        typeof value === "string" &&
        /^[1-9][0-9]*$/.test(value) &&
        Number.isSafeInteger(Number(value))
    ) {
        return Number(value);
    }
    return null;
}

function nonNegativeInteger(value) {
    return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function mediaDimension(value) {
    return Number.isSafeInteger(value) && value > 0 ? value : 0;
}

export function isRenderableConversation(value) {
    return Boolean(
        isPlainObject(value) &&
            Object.prototype.hasOwnProperty.call(value, "channel_id") &&
            Number.isSafeInteger(value.channel_id) &&
            value.channel_id > 0
    );
}

const RESPONSIBILITY_SCOPES = new Set(["all", "mine", "unassigned"]);

export function isResponsibilityScope(value) {
    return RESPONSIBILITY_SCOPES.has(value);
}

export function conversationResponsibility(conversation, currentUserId) {
    if (!isRenderableConversation(conversation)) {
        return "unknown";
    }
    const responsible = Object.prototype.hasOwnProperty.call(
        conversation,
        "responsible"
    )
        ? conversation.responsible
        : undefined;
    if (responsible === false) {
        return "unassigned";
    }
    const responsibleId =
        isPlainObject(responsible) &&
        Object.prototype.hasOwnProperty.call(responsible, "id") &&
        Number.isSafeInteger(responsible.id) &&
        responsible.id > 0
            ? responsible.id
            : false;
    const userId =
        Number.isSafeInteger(currentUserId) && currentUserId > 0
            ? currentUserId
            : false;
    if (!responsibleId) {
        return "unknown";
    }
    return responsibleId === userId ? "mine" : "assigned";
}

export function filterConversationsByResponsibility(
    conversations,
    scope,
    currentUserId
) {
    if (!Array.isArray(conversations) || !isResponsibilityScope(scope)) {
        return [];
    }
    const seen = new Set();
    const renderable = conversations.filter((conversation) => {
        if (!isRenderableConversation(conversation)) {
            return false;
        }
        if (seen.has(conversation.channel_id)) {
            return false;
        }
        seen.add(conversation.channel_id);
        return true;
    });
    if (scope === "all") {
        return renderable;
    }
    return renderable.filter(
        (conversation) =>
            conversationResponsibility(conversation, currentUserId) === scope
    );
}

function copySafeOwnProperties(value) {
    const result = {};
    for (const key of Object.keys(value)) {
        if (!DANGEROUS_PROPERTY_NAMES.has(key)) {
            result[key] = value[key];
        }
    }
    return result;
}

function safeLocalUrl(value) {
    if (typeof value !== "string") {
        return "";
    }
    const candidate = value.trim();
    return candidate.startsWith("/") &&
        !candidate.startsWith("//") &&
        !candidate.includes("\\") &&
        !Array.from(candidate).some((character) => {
            const code = character.charCodeAt(0);
            return code <= 31 || code === 127;
        })
        ? candidate
        : "";
}

function boundedText(value, limit) {
    return typeof value === "string" ? value.trim().slice(0, limit) : "";
}

export function normalizePartnerCompany(value) {
    if (!isPlainObject(value)) {
        return false;
    }
    const id = positiveInteger(value.id);
    const name = boundedText(value.name, 256);
    if (!id || !name) {
        return false;
    }
    return {
        id,
        name,
        email: boundedText(value.email, 320),
        phone: boundedText(value.phone, 80),
        vat: boundedText(value.vat, 80),
    };
}

export function partnerCompanyForIdentity(identity) {
    if (
        !isPlainObject(identity) ||
        !isPlainObject(identity.partner) ||
        !Object.prototype.hasOwnProperty.call(identity.partner, "company")
    ) {
        return false;
    }
    return normalizePartnerCompany(identity.partner.company);
}

function groupCount(value) {
    if (value === false) {
        return false;
    }
    return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function groupAvatarUrl(value) {
    if (value === false) {
        return false;
    }
    return safeLocalUrl(value) || null;
}

function odooDateTime(value) {
    if (value === false) {
        return false;
    }
    if (typeof value !== "string") {
        return null;
    }
    const candidate = value.trim();
    const match = ODOO_DATETIME_PATTERN.exec(candidate);
    if (!match) {
        return null;
    }
    const [, year, month, day, hour, minute, second] = match.map(Number);
    const lastDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
    return year >= 1000 &&
        month >= 1 &&
        month <= 12 &&
        day >= 1 &&
        day <= lastDay &&
        hour <= 23 &&
        minute <= 59 &&
        second <= 59
        ? candidate
        : null;
}

function attributionKey(value) {
    const candidate = boundedText(value, 128);
    return ATTRIBUTION_KEY_PATTERN.test(candidate) ? candidate : null;
}

function normalizeAttributionItem(value) {
    if (!isPlainObject(value) || !UUID_PATTERN.test(value.public_ref || "")) {
        return null;
    }
    if (
        !ATTRIBUTION_TOUCHPOINT_TYPES.has(value.touchpoint_type) ||
        !ATTRIBUTION_EVIDENCE_LEVELS.has(value.evidence_level)
    ) {
        return null;
    }
    const occurredAt = odooDateTime(value.occurred_at);
    const network = attributionKey(value.network);
    if (!occurredAt || !network || typeof value.show_ad_attribution !== "boolean") {
        return null;
    }
    const optionalKeys = {};
    for (const field of [
        "source_platform",
        "source_type",
        "entry_point_source",
        "entry_point_app",
    ]) {
        const raw = value[field];
        const normalized = raw === "" ? "" : attributionKey(raw);
        if (normalized === null) {
            return null;
        }
        optionalKeys[field] = normalized;
    }
    const boundedFields = {};
    for (const field of [
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "creative_media_type",
    ]) {
        if (typeof value[field] !== "string" || value[field].length > 512) {
            return null;
        }
        boundedFields[field] = value[field].trim();
    }
    return {
        public_ref: value.public_ref.toLowerCase(),
        touchpoint_type: value.touchpoint_type,
        evidence_level: value.evidence_level,
        network,
        ...optionalKeys,
        ...boundedFields,
        show_ad_attribution: value.show_ad_attribution,
        occurred_at: occurredAt,
    };
}

/**
 * Fail-closed allow-list for the optional attribution UiDTO.
 *
 * Provider identifiers, URLs, hashes and extension objects are intentionally absent.
 *
 * @param {Object} value server projection envelope
 * @returns {Object|null} bounded renderable projection, or null when malformed
 */
export function normalizeAttributionProjection(value) {
    if (!isPlainObject(value) || typeof value.enabled !== "boolean") {
        return null;
    }
    if (!value.enabled) {
        return {enabled: false, items: [], has_more: false, next_cursor: false};
    }
    if (
        !Array.isArray(value.items) ||
        value.items.length > 20 ||
        typeof value.has_more !== "boolean" ||
        (value.next_cursor !== false &&
            (typeof value.next_cursor !== "string" ||
                !UUID_PATTERN.test(value.next_cursor))) ||
        (value.has_more && value.next_cursor === false)
    ) {
        return null;
    }
    const items = value.items.map(normalizeAttributionItem).filter(Boolean);
    if (value.items.length && !items.length) {
        return null;
    }
    return {
        enabled: true,
        items,
        has_more: value.has_more,
        next_cursor: value.next_cursor ? value.next_cursor.toLowerCase() : false,
    };
}

/**
 * Allow-list the optional group projection before it reaches render helpers.
 *
 * A missing or malformed field invalidates the complete optional projection;
 * conversation policy remains independently read-only based on its type.
 *
 * @param {Object} conversation conversation UiDTO item
 * @returns {Object|Boolean} allow-listed metadata or false
 */
export function normalizeGroupMetadata(conversation) {
    if (!isPlainObject(conversation) || !isGroupConversation(conversation)) {
        return false;
    }
    const source = conversation.group;
    if (
        !isPlainObject(source) ||
        !GROUP_METADATA_FIELDS.every((field) =>
            Object.prototype.hasOwnProperty.call(source, field)
        )
    ) {
        return false;
    }
    const displayName = boundedText(source.display_name, 160);
    const avatarUrl = groupAvatarUrl(source.avatar_url);
    const participantCount = groupCount(source.participant_count);
    const adminCount = groupCount(source.admin_count);
    const lastSyncedAt = odooDateTime(source.last_synced_at);
    if (
        !displayName ||
        avatarUrl === null ||
        participantCount === null ||
        adminCount === null ||
        (participantCount !== false &&
            adminCount !== false &&
            adminCount > participantCount) ||
        !GROUP_ROLES.has(source.own_role) ||
        !GROUP_METADATA_STATES.has(source.metadata_state) ||
        lastSyncedAt === null
    ) {
        return false;
    }
    return {
        display_name: displayName,
        avatar_url: avatarUrl,
        participant_count: participantCount,
        admin_count: adminCount,
        own_role: source.own_role,
        metadata_state: source.metadata_state,
        last_synced_at: lastSyncedAt,
    };
}

export function groupMetadataUi(conversation) {
    if (!isGroupConversation(conversation)) {
        return false;
    }
    return (
        normalizeGroupMetadata(conversation) || {
            display_name: boundedText(conversation.name, 160) || "Grupo",
            avatar_url: false,
            participant_count: false,
            admin_count: false,
            own_role: "unknown",
            metadata_state: "unavailable",
            last_synced_at: false,
        }
    );
}

/**
 * Return the same-origin avatar projection that may be rendered for a conversation.
 *
 * Group metadata remains authoritative for group conversations. Direct conversations
 * may use the avatar projected by their canonical identity, but provider URLs and
 * inherited properties never reach an image element.
 *
 * @param {Object} conversation conversation UiDTO item
 * @returns {String|Boolean} safe local URL or false
 */
export function conversationAvatarUrl(conversation) {
    if (!isPlainObject(conversation)) {
        return false;
    }
    const group = groupMetadataUi(conversation);
    if (group) {
        return group.avatar_url || false;
    }
    const identity = Object.prototype.hasOwnProperty.call(conversation, "identity")
        ? conversation.identity
        : false;
    if (
        !isPlainObject(identity) ||
        !Object.prototype.hasOwnProperty.call(identity, "avatar_url")
    ) {
        return false;
    }
    return safeLocalUrl(identity.avatar_url) || false;
}

export function conversationDisplayName(conversation) {
    const group = groupMetadataUi(conversation);
    if (group) {
        return group.display_name;
    }
    return conversation && typeof conversation.name === "string"
        ? conversation.name
        : "";
}

export function normalizeConversationGroup(conversation) {
    if (!isPlainObject(conversation)) {
        return conversation;
    }
    const hasGroup = Object.prototype.hasOwnProperty.call(conversation, "group");
    if (!isGroupConversation(conversation) && !hasGroup) {
        return conversation;
    }
    const item = copySafeOwnProperties(conversation);
    item.group = isGroupConversation(conversation)
        ? normalizeGroupMetadata(conversation)
        : false;
    return item;
}

export function groupMetadataStateMeta(state) {
    return (
        GROUP_METADATA_STATE_METADATA[state] ||
        GROUP_METADATA_STATE_METADATA.unavailable
    );
}

export function groupRoleMeta(role) {
    return GROUP_ROLE_METADATA[role] || GROUP_ROLE_METADATA.unknown;
}

function normalizeMediaDescriptor(value) {
    if (!isPlainObject(value)) {
        return null;
    }
    const kind = MEDIA_KINDS.includes(value.kind) ? value.kind : "document";
    const contentUrl = safeLocalUrl(value.content_url);
    const state = MEDIA_STATES.has(value.state)
        ? value.state
        : contentUrl
        ? "ready"
        : "pending";
    return {
        id: positiveInteger(value.id) || false,
        kind,
        state,
        name: typeof value.name === "string" ? value.name : "",
        mimetype: typeof value.mimetype === "string" ? value.mimetype : "",
        size_bytes:
            Number.isSafeInteger(value.size_bytes) && value.size_bytes >= 0
                ? value.size_bytes
                : 0,
        content_url: contentUrl,
        download_url: safeLocalUrl(value.download_url),
        is_voice_note: value.is_voice_note === true,
        duration_seconds:
            Number.isFinite(value.duration_seconds) && value.duration_seconds >= 0
                ? value.duration_seconds
                : 0,
        width: mediaDimension(value.width),
        height: mediaDimension(value.height),
    };
}

function normalizeReaction(value) {
    if (!isPlainObject(value) || typeof value.emoji !== "string") {
        return null;
    }
    const emoji = value.emoji.trim();
    if (!emoji || Array.from(emoji).length > 16) {
        return null;
    }
    const count = positiveInteger(value.count) || 0;
    if (!count) {
        return null;
    }
    return {
        emoji,
        count,
        reacted_by_me: value.reacted_by_me === true,
    };
}

function normalizeActions(value, isDeleted) {
    const actions = isPlainObject(value) ? value : {};
    return {
        reply: !isDeleted && actions.reply === true,
        react: !isDeleted && actions.react === true,
        edit: !isDeleted && actions.edit === true,
        delete: !isDeleted && actions.delete === true,
        resend: !isDeleted && actions.resend === true,
    };
}

function normalizeGroupDelivery(value) {
    if (
        !isPlainObject(value) ||
        !Number.isSafeInteger(value.delivered_count) ||
        value.delivered_count < 0 ||
        !Number.isSafeInteger(value.read_count) ||
        value.read_count < 0
    ) {
        return false;
    }
    return {
        delivered_count: value.delivered_count,
        read_count: value.read_count,
    };
}

function normalizeTimelineItem(value) {
    if (!isPlainObject(value)) {
        return null;
    }
    const messageId = positiveInteger(value.message_id);
    if (!messageId) {
        return null;
    }
    const item = copySafeOwnProperties(value);
    item.message_id = messageId;
    item.body_text = typeof value.body_text === "string" ? value.body_text : "";
    item.is_deleted = value.is_deleted === true;
    item.deleted_content_visible =
        item.is_deleted && value.deleted_content_visible === true;
    item.is_forwarded = value.is_forwarded === true;
    item.edited_at = typeof value.edited_at === "string" ? value.edited_at : "";
    item.media = (Array.isArray(value.media) ? value.media : [])
        .map(normalizeMediaDescriptor)
        .filter(Boolean);
    item.reactions = (Array.isArray(value.reactions) ? value.reactions : [])
        .map(normalizeReaction)
        .filter(Boolean);
    item.actions = normalizeActions(value.actions, item.is_deleted);
    item.dispatch_reason = DISPATCH_REASON_CODES.has(value.dispatch_reason)
        ? value.dispatch_reason
        : "";
    item.retry_of_message_id = positiveInteger(value.retry_of_message_id) || false;
    item.source_inbox_event_id = positiveInteger(value.source_inbox_event_id) || false;
    item.group_delivery = normalizeGroupDelivery(value.group_delivery);
    if (item.is_deleted && !item.deleted_content_visible) {
        item.body_text = "";
        item.media = [];
        item.reactions = [];
        item.reply_to = false;
    }
    return item;
}

function healthState(value) {
    const candidate = typeof value === "string" ? value.trim().toLocaleLowerCase() : "";
    // Older providers may expose their transport-specific logged_out state.
    // Keep that detail outside the UiDTO while degrading safely in the client.
    if (candidate === "logged_out") {
        return "authentication_required";
    }
    return candidate === "checking" || CONNECTION_HEALTH_STATES.includes(candidate)
        ? candidate
        : "unknown";
}

function healthText(value, limit = 160) {
    return typeof value === "string" ? value.trim().slice(0, limit) : "";
}

function normalizeConnectionHealthItem(value) {
    if (!isPlainObject(value)) {
        return null;
    }
    const id = positiveInteger(value.id);
    if (!id) {
        return null;
    }
    const state = healthState(value.state);
    const connectionRole = healthText(value.connection_role, 24);
    return {
        id,
        account_id: positiveInteger(value.account_id) || false,
        account_name: healthText(value.account_name, 120),
        connection_name: healthText(value.connection_name, 120),
        display_address: healthText(value.display_address, 120),
        platform: healthText(value.platform, 40),
        provider: healthText(value.provider, 40),
        connection_role: CONNECTION_ROLES.has(connectionRole)
            ? connectionRole
            : "standby",
        accepts_inbound: value.accepts_inbound === true,
        unsupported_count: nonNegativeInteger(value.unsupported_count),
        state: state === "checking" ? "unknown" : state,
        checking: value.checking === true || state === "checking",
        last_check_at: healthText(value.last_check_at, 40),
        state_changed_at: healthText(value.state_changed_at, 40),
        detail: healthText(value.detail, 240),
        can_check: value.can_check === true,
    };
}

function connectionHealthNotification(payload) {
    if (!Number.isSafeInteger(payload.connection_id) || payload.connection_id <= 0) {
        return null;
    }
    if (!Object.prototype.hasOwnProperty.call(payload, "item")) {
        return payload;
    }
    if (
        isPlainObject(payload.item) &&
        positiveInteger(payload.item.id) === payload.connection_id
    ) {
        return payload;
    }
    const invalidation = copySafeOwnProperties(payload);
    delete invalidation.item;
    return invalidation;
}

function latestHealthCheck(items, fallback) {
    return (
        items
            .map((item) => item.last_check_at)
            .filter(Boolean)
            .concat(healthText(fallback, 40) || [])
            .sort()
            .pop() || ""
    );
}

function healthSummary(items, supplied = {}) {
    const summary = Object.fromEntries(
        CONNECTION_HEALTH_STATES.map((state) => [state, 0])
    );
    summary.checking = 0;
    summary.advisory = 0;
    for (const item of items) {
        summary[item.state] += 1;
        if (item.checking) {
            summary.checking += 1;
        }
        if (item.detail.toLocaleLowerCase() === "metadata_limited") {
            summary.advisory += 1;
        }
    }
    return {
        total: items.length,
        ...summary,
        last_check_at: latestHealthCheck(items, supplied.last_check_at),
        can_check: items.some((item) => item.can_check),
    };
}

function canonicalUuid(value) {
    if (typeof value !== "string") {
        return "";
    }
    const candidate = value.trim().toLocaleLowerCase();
    return UUID_PATTERN.test(candidate) ? candidate : "";
}

function uuidFromBytes(source) {
    const bytes = new Uint8Array(16);
    for (let index = 0; index < bytes.length; index += 1) {
        const value = Number(source[index]);
        bytes[index] = Number.isFinite(value) ? value & 0xff : 0;
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
    return [
        hex.slice(0, 4).join(""),
        hex.slice(4, 6).join(""),
        hex.slice(6, 8).join(""),
        hex.slice(8, 10).join(""),
        hex.slice(10, 16).join(""),
    ].join("-");
}

function byteFromRandom(random) {
    let value = 0;
    try {
        value = Number(random());
    } catch {
        value = 0;
    }
    if (!Number.isFinite(value)) {
        return 0;
    }
    return Math.floor(Math.min(Math.max(value, 0), 0.9999999999999999) * 256);
}

export function normalizeSearchText(value) {
    if (typeof value !== "string") {
        return "";
    }
    return value
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .toLocaleLowerCase()
        .trim()
        .replace(/\s+/g, " ");
}

export function normalizeMessageMedia(value) {
    return (Array.isArray(value) ? value : [])
        .map(normalizeMediaDescriptor)
        .filter(Boolean);
}

export function formatFileSize(value) {
    if (!Number.isFinite(value) || value < 0) {
        return "";
    }
    if (value < 1024) {
        return `${Math.round(value)} B`;
    }
    if (value < 1024 * 1024) {
        return `${(value / 1024).toFixed(value < 10 * 1024 ? 1 : 0)} KB`;
    }
    return `${(value / (1024 * 1024)).toFixed(value < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

export function mediaKindFromFile(file) {
    const mimetype =
        file && typeof file.type === "string" ? file.type.toLocaleLowerCase() : "";
    if (mimetype.startsWith("image/")) {
        return "image";
    }
    if (mimetype.startsWith("audio/")) {
        return "audio";
    }
    if (mimetype.startsWith("video/")) {
        return "video";
    }
    if (mimetype && mimetype !== "application/octet-stream") {
        return "document";
    }
    const name = file && typeof file.name === "string" ? file.name : "";
    const extension = name.split(".").pop().toLocaleLowerCase();
    if (["jpg", "jpeg", "png", "gif", "webp"].includes(extension)) {
        return "image";
    }
    if (["aac", "m4a", "mp3", "oga", "ogg", "opus", "wav"].includes(extension)) {
        return "audio";
    }
    if (["3gp", "m4v", "mov", "mp4", "webm"].includes(extension)) {
        return "video";
    }
    return "document";
}

function validMimetypeArray(value, allowEmpty = true) {
    return Boolean(
        Array.isArray(value) &&
            (allowEmpty || value.length) &&
            !value.some((item) => typeof item !== "string" || !item.trim())
    );
}

function validRecordingCapability(config) {
    const recordingMimetypes = config.recording_mimetypes;
    const voiceNoteMimetypes = config.voice_note_mimetypes;
    const maxDurationSeconds = config.max_duration_seconds;
    const hasContract =
        recordingMimetypes !== undefined ||
        voiceNoteMimetypes !== undefined ||
        maxDurationSeconds !== undefined;
    return Boolean(
        !hasContract ||
            (validMimetypeArray(recordingMimetypes, false) &&
                validMimetypeArray(voiceNoteMimetypes) &&
                Number.isSafeInteger(maxDurationSeconds) &&
                maxDurationSeconds > 0)
    );
}

function structuredMediaCapability(config) {
    if (config === true) {
        return {enabled: true};
    }
    if (!isPlainObject(config)) {
        return false;
    }
    if (
        Object.prototype.hasOwnProperty.call(config, "enabled") &&
        typeof config.enabled !== "boolean"
    ) {
        return false;
    }
    if (config.enabled === false) {
        return false;
    }
    if (
        config.max_bytes !== undefined &&
        config.max_bytes !== null &&
        (!Number.isSafeInteger(config.max_bytes) || config.max_bytes <= 0)
    ) {
        return false;
    }
    if (
        Object.prototype.hasOwnProperty.call(config, "caption") &&
        typeof config.caption !== "boolean"
    ) {
        return false;
    }
    const mimetypes = config.mimetypes || [];
    if (!validMimetypeArray(mimetypes)) {
        return false;
    }
    if (!validRecordingCapability(config)) {
        return false;
    }
    return {...copySafeOwnProperties(config), enabled: true};
}

export function mediaCapabilities(capabilities) {
    const source = isPlainObject(capabilities) ? capabilities.media : {};
    const result = {};
    if (!isPlainObject(source)) {
        return result;
    }
    for (const kind of MEDIA_KINDS) {
        const normalized = structuredMediaCapability(source[kind]);
        if (normalized) {
            result[kind] = normalized;
        }
    }
    return result;
}

export function mediaCaptionAllowed(capabilities, kind) {
    const config = mediaCapabilities(capabilities)[kind];
    return Boolean(config && config.caption !== false);
}

function conversationActionCapability(isDirect, isGroup, allowSend, capabilities, key) {
    return Boolean(isDirect || (isGroup && allowSend && capabilities[key] === true));
}

export function conversationUiPolicy(conversation) {
    const exists = Boolean(conversation && typeof conversation === "object");
    const isGroup = isGroupConversation(conversation);
    const isDirect = Boolean(exists && conversation.conversation_type === "direct");
    const allowSend = Boolean((isDirect || isGroup) && conversation.can_send === true);
    const capabilities =
        exists && isPlainObject(conversation.capabilities)
            ? conversation.capabilities
            : {};
    return {
        is_group: isGroup,
        show_composer: Boolean(isDirect || (isGroup && allowSend)),
        allow_send: allowSend,
        allow_reply: conversationActionCapability(
            isDirect,
            isGroup,
            allowSend,
            capabilities,
            "reply"
        ),
        allow_attachments: Boolean(
            (isDirect || isGroup) &&
                allowSend &&
                Object.keys(mediaCapabilities(capabilities)).length
        ),
        allow_react: conversationActionCapability(
            isDirect,
            isGroup,
            allowSend,
            capabilities,
            "react"
        ),
        allow_edit: conversationActionCapability(
            isDirect,
            isGroup,
            allowSend,
            capabilities,
            "edit_message"
        ),
        allow_delete: conversationActionCapability(
            isDirect,
            isGroup,
            allowSend,
            capabilities,
            "delete_message"
        ),
    };
}

export function messageActionEnabled(conversation, message, action) {
    const policyKey = MESSAGE_ACTION_POLICY_KEYS[action];
    if (!policyKey || !isPlainObject(message) || !isPlainObject(message.actions)) {
        return false;
    }
    return Boolean(
        conversationUiPolicy(conversation)[policyKey] &&
            message.actions[action] === true
    );
}

/**
 * Gate the diagnostic source-webhook action on both server authorization and
 * a normalized inbox-event reference. Exact booleans and positive integers
 * keep malformed or stale DTOs fail-closed.
 *
 * @param {Object} capabilities bootstrap capabilities
 * @param {Object} message timeline message DTO
 * @returns {Boolean} whether the source webhook may be opened
 */
export function sourceWebhookActionEnabled(capabilities, message) {
    return Boolean(
        isPlainObject(capabilities) &&
            capabilities.view_source_webhook === true &&
            isPlainObject(message) &&
            positiveInteger(message.source_inbox_event_id)
    );
}

function mediaMaxBytes(kind, config, capabilities) {
    for (const value of [
        config.max_bytes,
        isPlainObject(capabilities) && capabilities.max_media_bytes,
    ]) {
        const limit = Number(value);
        if (Number.isSafeInteger(limit) && limit > 0) {
            return limit;
        }
    }
    return DEFAULT_MAX_MEDIA_BYTES[kind];
}

function mediaMimetypeAllowed(file, config) {
    const allowed = Array.isArray(config.mimetypes)
        ? config.mimetypes.filter((item) => typeof item === "string")
        : [];
    return (
        !allowed.length ||
        typeof file.type !== "string" ||
        !file.type ||
        allowed.includes(file.type)
    );
}

export function validateMediaFile(file, capabilities) {
    if (!file || typeof file.name !== "string") {
        return {ok: false, error: "Selecione um arquivo válido."};
    }
    const available = mediaCapabilities(capabilities);
    const kind = mediaKindFromFile(file);
    const config = available[kind];
    if (!config) {
        return {
            ok: false,
            error: "Este tipo de arquivo não está disponível nesta conversa.",
        };
    }
    const size = Number(file.size);
    if (!Number.isSafeInteger(size) || size <= 0) {
        return {ok: false, error: "O arquivo está vazio ou não pôde ser lido."};
    }
    const maxBytes = mediaMaxBytes(kind, config, capabilities);
    if (size > maxBytes) {
        return {
            ok: false,
            error: `O arquivo excede o limite de ${formatFileSize(maxBytes)}.`,
        };
    }
    if (!mediaMimetypeAllowed(file, config)) {
        return {
            ok: false,
            error: "O formato deste arquivo não é aceito pelo provedor.",
        };
    }
    return {ok: true, kind, max_bytes: maxBytes};
}

export function messagePreviewText(message) {
    if (!isPlainObject(message)) {
        return "Mensagem";
    }
    if (message.is_deleted) {
        return "Mensagem apagada";
    }
    if (typeof message.body_text === "string" && message.body_text.trim()) {
        return message.body_text;
    }
    const media = normalizeMessageMedia(message.media)[0];
    const labels = {
        image: "Imagem",
        audio: media && media.is_voice_note ? "Mensagem de voz" : "Áudio",
        video: "Vídeo",
        document: "Documento",
    };
    return (media && labels[media.kind]) || "Mensagem";
}

export function initials(value) {
    if (typeof value !== "string") {
        return "?";
    }
    const words = value.match(WORD_PATTERN) || [];
    if (!words.length) {
        return "?";
    }
    if (words.length === 1) {
        return Array.from(words[0]).slice(0, 2).join("").toLocaleUpperCase();
    }
    return `${Array.from(words[0])[0]}${
        Array.from(words[words.length - 1])[0]
    }`.toLocaleUpperCase();
}

export function validateEnvelope(payload) {
    if (!isPlainObject(payload)) {
        throw new TypeError("Contact Center payload must be a plain object");
    }
    if (!Number.isInteger(payload.schema_version)) {
        throw new TypeError(
            "Contact Center payload requires an integer schema_version"
        );
    }
    if (payload.schema_version !== SUPPORTED_SCHEMA_VERSION) {
        throw new RangeError(
            `Unsupported Contact Center schema version: ${payload.schema_version}`
        );
    }
    return payload;
}

/**
 * Merge timeline pages or live updates without mutating either input.
 *
 * When prepending an older page, existing items win on an overlap because they
 * may already contain fresher delivery state. For append/live updates, incoming
 * fields win while fields omitted by a partial notification are preserved.
 *
 * @param {Array<Object>} existing current timeline items
 * @param {Array<Object>} incoming older page or live updates
 * @param {Object} options merge options
 * @param {Boolean} options.prepend whether incoming contains an older page
 * @returns {Array<Object>} normalized items sorted by message ID
 */
export function mergeTimelineItems(existing, incoming, {prepend = false} = {}) {
    const currentItems = Array.isArray(existing) ? existing : [];
    const incomingItems = Array.isArray(incoming) ? incoming : [];
    const orderedSources = prepend
        ? incomingItems.concat(currentItems)
        : currentItems.concat(incomingItems);
    const byMessageId = new Map();

    for (const source of orderedSources) {
        if (!isPlainObject(source)) {
            continue;
        }
        const messageId = positiveInteger(source.message_id);
        if (!messageId) {
            continue;
        }
        const previous = byMessageId.get(messageId) || {};
        const merged = Object.assign(
            {},
            copySafeOwnProperties(previous),
            copySafeOwnProperties(source),
            {message_id: messageId}
        );
        byMessageId.set(messageId, merged);
    }

    return Array.from(byMessageId.values())
        .map(normalizeTimelineItem)
        .filter(Boolean)
        .sort((left, right) => left.message_id - right.message_id);
}

export function deliveryMeta(state) {
    const key = typeof state === "string" ? state.trim().toLocaleLowerCase() : "";
    if (!key) {
        return EMPTY_DELIVERY_METADATA;
    }
    return DELIVERY_METADATA[key] || UNKNOWN_DELIVERY_METADATA;
}

export function messageStatusMeta(deliveryState, dispatchState) {
    const dispatchKey =
        typeof dispatchState === "string"
            ? dispatchState.trim().toLocaleLowerCase()
            : "";
    const delivery = deliveryMeta(deliveryState);
    if (DISPATCH_METADATA[dispatchKey]) {
        // Authenticated provider evidence is stronger than a stale local
        // dispatch outcome. This also keeps historical rows truthful while the
        // backend reconciliation that closes their outbox is being applied.
        if (delivery.rank >= DELIVERY_METADATA.sent.rank) {
            return delivery;
        }
        return DISPATCH_METADATA[dispatchKey];
    }
    return delivery;
}

export function messageDispatchReasonMeta(reason) {
    const code =
        typeof reason === "string" && DISPATCH_REASON_CODES.has(reason.trim())
            ? reason.trim()
            : "";
    return DISPATCH_REASON_METADATA[code] || EMPTY_DISPATCH_REASON_METADATA;
}

export function realtimeStatusMeta(state) {
    const key = typeof state === "string" ? state.trim().toLocaleLowerCase() : "";
    return REALTIME_STATUS_METADATA[key] || REALTIME_STATUS_METADATA.connecting;
}

export function conversationStateMeta(state) {
    const key = typeof state === "string" ? state.trim().toLocaleLowerCase() : "";
    return (
        CONVERSATION_STATE_METADATA[key] ||
        Object.freeze({key, label: "", tone: "muted"})
    );
}

export function conversationResolutionAction(conversation) {
    const state =
        conversation && typeof conversation.state === "string"
            ? conversation.state.trim().toLocaleLowerCase()
            : "";
    return CONVERSATION_RESOLUTION_ACTIONS[state] || false;
}

export function connectionHealthStateMeta(state) {
    return CONNECTION_HEALTH_METADATA[healthState(state)];
}

export function connectionHealthDetail(item) {
    const normalized = normalizeConnectionHealthItem(item);
    if (!normalized) {
        return "";
    }
    const detail = normalized.detail.toLocaleLowerCase();
    if (normalized.state === "unknown") {
        return connectionHealthStateMeta("unknown").description;
    }
    if (CONNECTION_HEALTH_DETAIL_LABELS[detail]) {
        return CONNECTION_HEALTH_DETAIL_LABELS[detail];
    }
    if (normalized.detail && !/^[a-z0-9_.-]+$/.test(normalized.detail)) {
        return normalized.detail;
    }
    return connectionHealthStateMeta(normalized.state).description;
}

export function connectionHealthNeedsAttention(item) {
    const normalized = normalizeConnectionHealthItem(item);
    return Boolean(normalized && normalized.state !== "connected");
}

export function normalizeConnectionHealth(value) {
    if (!isPlainObject(value)) {
        return false;
    }
    const byId = new Map();
    for (const source of Array.isArray(value.items) ? value.items : []) {
        const item = normalizeConnectionHealthItem(source);
        if (item) {
            byId.set(item.id, item);
        }
    }
    const items = Array.from(byId.values()).sort((left, right) => {
        const priority =
            connectionHealthStateMeta(left.state).priority -
            connectionHealthStateMeta(right.state).priority;
        if (priority) {
            return priority;
        }
        return (left.account_name || left.connection_name).localeCompare(
            right.account_name || right.connection_name
        );
    });
    return {
        summary: healthSummary(
            items,
            isPlainObject(value.summary) ? value.summary : {}
        ),
        items,
    };
}

export function mergeConnectionHealth(current, incomingItem) {
    const normalized = normalizeConnectionHealth(current);
    const candidate = normalizeConnectionHealthItem(incomingItem);
    if (!normalized || !candidate) {
        return false;
    }
    const previous = normalized.items.find((entry) => entry.id === candidate.id);
    // Realtime is only an invalidation hint. An item absent from the scoped
    // bootstrap may belong to another active-company context or may have just
    // been created; force a scoped refresh instead of inserting it blindly.
    if (!previous) {
        return false;
    }
    const source = copySafeOwnProperties(incomingItem);
    const item = normalizeConnectionHealthItem({
        ...previous,
        ...source,
        id: candidate.id,
    });
    return normalizeConnectionHealth({
        summary: normalized.summary,
        items: normalized.items.filter((entry) => entry.id !== item.id).concat(item),
    });
}

export function connectionFleetMeta(value) {
    const health = normalizeConnectionHealth(value);
    if (!health || !health.summary.total) {
        return {
            state: "unknown",
            label: "Nenhuma conexão",
            description: "Nenhuma conexão está disponível nesta caixa de entrada.",
        };
    }
    const {summary} = health;
    const state =
        CONNECTION_HEALTH_ALERT_ORDER.find((candidate) => summary[candidate] > 0) ||
        "connected";
    const checkingSuffix = summary.checking ? ` · verificando ${summary.checking}` : "";
    const advisoryDescription = `${summary.advisory} ${
        summary.advisory === 1 ? "conexão operacional" : "conexões operacionais"
    } com metadados opcionais limitados.`;
    return {
        state,
        label: `${summary.connected}/${summary.total} conectadas${checkingSuffix}`,
        description:
            state === "connected"
                ? summary.advisory
                    ? advisoryDescription
                    : `Todas as ${summary.total} conexões estão disponíveis.`
                : connectionHealthStateMeta(state).description,
    };
}

export function contactCenterNotifications(detail) {
    if (!Array.isArray(detail)) {
        return [];
    }
    const events = [];
    for (const notification of detail) {
        if (
            !isPlainObject(notification) ||
            notification.type !== CONTACT_CENTER_NOTIFICATION_TYPE ||
            !isPlainObject(notification.payload)
        ) {
            continue;
        }
        const payload = notification.payload;
        try {
            validateEnvelope(payload);
        } catch {
            continue;
        }
        if (
            typeof payload.event_type !== "string" ||
            !EVENT_TYPE_PATTERN.test(payload.event_type)
        ) {
            continue;
        }
        if (payload.event_type === CONNECTION_HEALTH_EVENT_TYPE) {
            const healthPayload = connectionHealthNotification(payload);
            if (healthPayload) {
                events.push(healthPayload);
            }
            continue;
        } else if (
            !Number.isSafeInteger(payload.channel_id) ||
            payload.channel_id <= 0
        ) {
            continue;
        }
        events.push(payload);
    }
    return events;
}

export function makeClientRequestId(cryptoLike) {
    if (cryptoLike && typeof cryptoLike.randomUUID === "function") {
        try {
            const requestId = canonicalUuid(cryptoLike.randomUUID());
            if (requestId) {
                return requestId;
            }
        } catch {
            // Continue with a deterministic byte-based UUID fallback.
        }
    }

    if (cryptoLike && typeof cryptoLike.getRandomValues === "function") {
        try {
            const bytes = new Uint8Array(16);
            const generated = cryptoLike.getRandomValues(bytes);
            return uuidFromBytes(
                generated && generated.length === 16 ? generated : bytes
            );
        } catch {
            // Continue with an injectable pseudo-random fallback.
        }
    }

    const injectedRandom =
        typeof cryptoLike === "function"
            ? cryptoLike
            : cryptoLike && typeof cryptoLike.random === "function"
            ? cryptoLike.random.bind(cryptoLike)
            : Math.random;
    return uuidFromBytes(
        Array.from({length: 16}, () => byteFromRandom(injectedRandom))
    );
}
