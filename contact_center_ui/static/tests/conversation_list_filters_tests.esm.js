/** @odoo-module **/

/* global QUnit */

import {click, getFixture, mount, nextTick} from "@web/../tests/helpers/utils";
import {ContactCenterStore} from "@contact_center_ui/js/contact_center_store.esm";
import {ConversationList} from "@contact_center_ui/js/conversation_list.esm";
import {MessageComposer} from "@contact_center_ui/js/message_composer.esm";
import {SUPPORTED_SCHEMA_VERSION} from "@contact_center_ui/js/contact_center_model.esm";
import {browser} from "@web/core/browser/browser";
import {hotkeyService} from "@web/core/hotkeys/hotkey_service";
import {makeFakeLocalizationService} from "@web/../tests/helpers/mock_services";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";
import {reactive} from "@odoo/owl";
import {registry} from "@web/core/registry";
import {uiService} from "@web/core/ui/ui_service";

function filterStore() {
    const store = new ContactCenterStore({
        orm: {},
        busService: new EventTarget(),
        notification: false,
        stateFactory: reactive,
    });
    store.state.bootstrap = {
        user: {id: 6},
        states: {
            conversation: [
                {key: "open", label: "Aberta"},
                {key: "resolved", label: "Resolvida"},
                {key: "archived", label: "Arquivada"},
            ],
        },
        accounts: [
            {
                id: 1,
                name: "Comercial",
                can_start_conversation: true,
                start_phone_country: {code: "BR", name: "Brasil", calling_code: "55"},
            },
            {id: 2, name: "Atendimento", can_start_conversation: true},
        ],
        tags: [{id: 3, name: "Retorno"}],
        agents: [
            {id: 6, name: "Lucas"},
            {id: 7, name: "Ana"},
        ],
        capabilities: {followups: true},
    };
    store.state.listPhase = "ready";
    store.state.inboxDensity = "comfortable";
    store.loadConversations = async () => true;
    return store;
}

async function mountList(store) {
    registry.category("services").add("hotkey", hotkeyService);
    registry.category("services").add("ui", uiService);
    registry.category("services").add("dialog", {
        start: () => ({add: () => () => undefined}),
    });
    makeFakeLocalizationService();
    const env = await makeTestEnv();
    const target = getFixture();
    const list = await mount(ConversationList, target, {
        env,
        props: {state: store.state, store},
    });
    return {list, target};
}

QUnit.module("contact_center_ui > sidebar filter flyout", (hooks) => {
    hooks.beforeEach(() => {
        const banner = document.getElementById("oe_neutralize_banner");
        if (banner) {
            (banner.parentElement || banner).remove();
        }
    });

    QUnit.test(
        "the list opens with the saved view, groups and position and remembers changes",
        async (assert) => {
            const store = filterStore();
            const remembered = [];
            const rememberInboxLayout = store.rememberInboxLayout.bind(store);
            store.rememberInboxLayout = (patch) => {
                remembered.push(JSON.parse(JSON.stringify(patch)));
                return rememberInboxLayout(patch);
            };
            store.inboxLayout = {
                listView: "flat",
                collapsedInboxes: {"inbox:1": true},
                sidePanel: "contact",
            };
            store.pendingListScrollTop = 120;
            store.state.conversations = [
                {channel_id: 20, state: "open", account: {id: 1, name: "Comercial"}},
            ];
            try {
                const {list, target} = await mountList(store);
                assert.strictEqual(list.ui.view, "flat");
                assert.ok(
                    target
                        .querySelector("[aria-label='Lista sem agrupamento']")
                        .classList.contains("is-active")
                );
                assert.strictEqual(
                    store.pendingListScrollTop,
                    0,
                    "the saved position is applied once to the rendered page"
                );
                await click(target, "[aria-label='Agrupar por caixa']");
                assert.ok(list.inboxCollapsed("inbox:1"), "collapsed groups return");
                assert.notOk(list.inboxCollapsed("inbox:2"));
                list.toggleInbox("inbox:2");
                assert.deepEqual(remembered, [
                    {listView: "grouped"},
                    {collapsedInboxes: {"inbox:1": true, "inbox:2": true}},
                ]);
                assert.deepEqual(store.inboxLayout.collapsedInboxes, {
                    "inbox:1": true,
                    "inbox:2": true,
                });
                const viewport = list.viewportRef.el;
                viewport.dispatchEvent(new Event("scroll"));
                assert.strictEqual(
                    store.listScrollTop,
                    viewport.scrollTop,
                    "the position of this visit is kept for the document memory"
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "the restored window returns to its real scroll position",
        async (assert) => {
            const store = filterStore();
            store.inboxLayout = {
                listView: "flat",
                collapsedInboxes: {},
                sidePanel: "contact",
            };
            store.state.conversations = Array.from({length: 40}, (_value, index) => ({
                channel_id: index + 1,
                state: "open",
                name: `Conversa ${index + 1}`,
                account: {id: 1, name: "Comercial"},
            }));
            const target = getFixture();
            const originalStyle = target.style.cssText;
            target.style.cssText =
                "position:fixed;top:0;left:0;width:360px;height:420px;overflow:hidden";
            try {
                const {list} = await mountList(store);
                const viewport = list.viewportRef.el;
                // Outside the full inbox layout, give the list its own height.
                viewport.style.flex = "none";
                viewport.style.height = "200px";
                assert.ok(
                    viewport.scrollHeight > viewport.clientHeight + 300,
                    "the fixture list is scrollable"
                );
                store.pendingListScrollTop = 300;
                store.state.listScrollRestoreRequest += 1;
                await nextTick();
                assert.strictEqual(Math.round(viewport.scrollTop), 300);
                assert.strictEqual(store.pendingListScrollTop, 0);
                store.state.listScrollRestoreRequest += 1;
                await nextTick();
                assert.strictEqual(
                    Math.round(viewport.scrollTop),
                    300,
                    "a consumed position is not applied twice"
                );
            } finally {
                target.style.cssText = originalStyle;
                store.destroy();
            }
        }
    );

    QUnit.test(
        "bulk inbox disclosure includes empty boxes and follows mixed and flat views",
        async (assert) => {
            const store = filterStore();
            store.state.conversations = [
                {
                    channel_id: 20,
                    state: "open",
                    account: {id: 1, name: "Comercial"},
                },
            ];
            try {
                const {target} = await mountList(store);
                const bulk = () => target.querySelector(".cc-inbox-collapse-toggle");
                assert.strictEqual(bulk().title, "Recolher todas as caixas");
                assert.strictEqual(
                    bulk().getAttribute("aria-controls"),
                    "cc-inbox-group-2 cc-inbox-group-1"
                );
                await click(bulk());
                assert.strictEqual(bulk().title, "Expandir todas as caixas");
                assert.strictEqual(bulk().getAttribute("aria-expanded"), "false");
                assert.ok(bulk().querySelector(".fa-angle-double-down"));
                assert.strictEqual(
                    target.querySelectorAll(".cc-inbox-group__conversations[hidden]")
                        .length,
                    2
                );
                const emptyHeader = target.querySelector(
                    "[aria-controls='cc-inbox-group-2']"
                );
                await click(emptyHeader);
                assert.strictEqual(emptyHeader.getAttribute("aria-expanded"), "true");
                assert.strictEqual(
                    bulk().title,
                    "Recolher todas as caixas",
                    "a mixed state offers collapse"
                );
                await click(bulk());
                assert.strictEqual(
                    target.querySelectorAll(".cc-inbox-group__conversations[hidden]")
                        .length,
                    2
                );
                await click(target, "[aria-label='Lista sem agrupamento']");
                assert.notOk(bulk(), "the flat list has no box disclosure button");
                await click(target, "[aria-label='Agrupar por caixa']");
                assert.strictEqual(bulk().title, "Expandir todas as caixas");
                await click(bulk());
                assert.strictEqual(
                    target.querySelectorAll(".cc-inbox-group__conversations[hidden]")
                        .length,
                    0
                );
                assert.strictEqual(bulk().getAttribute("aria-expanded"), "true");
                store.state.bootstrap.accounts = [];
                store.state.conversations = [];
                await nextTick();
                assert.ok(
                    bulk().disabled,
                    "the control is disabled when there are no boxes"
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "bulk inbox disclosure preserves hidden filters and survives updates and paging",
        async (assert) => {
            const store = filterStore();
            const conversation = {
                channel_id: 20,
                state: "open",
                account: {id: 1, name: "Comercial"},
            };
            store.state.conversations = [conversation];
            try {
                const {list, target} = await mountList(store);
                await click(target, ".cc-inbox-collapse-toggle");
                store.state.filters.accountId = 1;
                await nextTick();
                await click(target, ".cc-inbox-collapse-toggle");
                assert.notOk(list.inboxCollapsed("inbox:1"));
                assert.ok(
                    list.inboxCollapsed("inbox:2"),
                    "the hidden box keeps its previous state"
                );
                store.state.filters.accountId = false;
                await nextTick();
                store.replaceConversation({...conversation, unread_count: 3});
                store.applyConversationPage(
                    {items: [{...conversation, channel_id: 21}]},
                    {reset: false, silent: true}
                );
                await nextTick();
                assert.notOk(list.inboxCollapsed("inbox:1"));
                assert.ok(list.inboxCollapsed("inbox:2"));
                assert.strictEqual(
                    target.querySelectorAll(".cc-inbox-group__conversations[hidden]")
                        .length,
                    1
                );
                await click(target, ".cc-inbox-collapse-toggle");
                store.replaceConversation({...conversation, unread_count: 4});
                store.applyConversationPage(
                    {items: [{...conversation, channel_id: 22}]},
                    {reset: false, silent: true}
                );
                await nextTick();
                assert.ok(
                    list.allInboxesCollapsed,
                    "new rows and unread changes never reopen boxes"
                );
                assert.strictEqual(
                    target.querySelectorAll(".cc-inbox-group__conversations[hidden]")
                        .length,
                    2
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "grouped rows omit the repeated inbox badge and keep conversation tags in both views",
        async (assert) => {
            const store = filterStore();
            const longName = "Retorno comercial com documentação complementar";
            store.state.conversations = [
                {
                    channel_id: 20,
                    state: "open",
                    name: "Cliente",
                    account: {id: 1, name: "Comercial"},
                    responsible: {id: 7, name: "Ana"},
                    tags: [
                        {id: 3, name: "Retorno", color: 2},
                        {id: 4, name: longName, color: 7},
                        {id: 3, name: "Duplicate"},
                        null,
                        {name: "Missing ID"},
                        {id: 5},
                    ],
                },
            ];
            try {
                const {target} = await mountList(store);
                assert.notOk(target.querySelector(".cc-inbox-badge"));
                assert.ok(
                    target
                        .querySelector("[data-channel-id='20']")
                        .closest(".cc-inbox-group")
                        .querySelector(".cc-inbox-group__identity")
                        .textContent.includes("Comercial"),
                    "the inbox remains identified by its group header"
                );
                const tags = target.querySelectorAll(".cc-conversation-tag");
                assert.strictEqual(tags.length, 2, "only valid, unique tags render");
                assert.strictEqual(tags[1].title, longName);
                assert.strictEqual(tags[1].querySelector("span").textContent, longName);
                assert.ok(tags[0].querySelector(".cc-tag-color--2"));
                assert.strictEqual(
                    target
                        .querySelector(".cc-conversation-item__tags")
                        .getAttribute("aria-label"),
                    "Marcadores da conversa"
                );
                await click(target, "[aria-label='Lista sem agrupamento']");
                assert.strictEqual(
                    target.querySelector(".cc-inbox-badge").title,
                    "Comercial"
                );
                assert.strictEqual(
                    target.querySelectorAll(".cc-conversation-tag").length,
                    2
                );
                await click(target, "[aria-label='Agrupar por caixa']");
                assert.notOk(target.querySelector(".cc-inbox-badge"));
                assert.strictEqual(
                    target.querySelectorAll(".cc-conversation-tag").length,
                    2
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "conversation tag changes update the mounted list without reloading it",
        async (assert) => {
            const store = filterStore();
            const conversation = {
                channel_id: 20,
                state: "open",
                name: "Cliente",
                account: {id: 1, name: "Comercial"},
                tags: [],
            };
            store.state.conversations = [conversation];
            store.loadConversations = () => {
                throw new Error("Tag updates must not reload the conversation list");
            };
            try {
                const {target} = await mountList(store);
                assert.notOk(target.querySelector(".cc-conversation-item__tags"));
                store.replaceConversation({
                    ...conversation,
                    tags: [{id: 3, name: "Retorno", color: 2}],
                });
                await nextTick();
                assert.strictEqual(
                    target.querySelector(".cc-conversation-tag").title,
                    "Retorno"
                );
                store.replaceConversation({
                    ...conversation,
                    tags: [{id: 4, name: "Urgente", color: 1}],
                });
                await nextTick();
                assert.strictEqual(
                    target.querySelectorAll(".cc-conversation-tag").length,
                    1
                );
                assert.strictEqual(
                    target.querySelector(".cc-conversation-tag").title,
                    "Urgente"
                );
                store.replaceConversation(conversation);
                await nextTick();
                assert.notOk(target.querySelector(".cc-conversation-item__tags"));
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "filters are grouped behind one button and states remain additive",
        async (assert) => {
            const store = filterStore();
            try {
                const {list, target} = await mountList(store);
                assert.notOk(target.querySelector(".cc-inbox-filter-panel"));
                assert.ok(target.querySelector(".cc-search-field input"));
                assert.strictEqual(
                    list.activeFilterCount,
                    1,
                    "the default open facet counts"
                );
                assert.strictEqual(list.filterSummary, "Aberta");
                assert.ok(
                    target.querySelector(".cc-inbox-header [role='status'].sr-only")
                );
                await click(target, ".cc-inbox-filter-toggle");
                assert.strictEqual(
                    target
                        .querySelector(".cc-inbox-filter-toggle")
                        .getAttribute("aria-expanded"),
                    "true"
                );
                assert.strictEqual(
                    target.querySelectorAll(".cc-inbox-filter-section").length,
                    6
                );
                await click(target, ".cc-state-option--resolved");
                assert.deepEqual(store.state.filters.states, ["open", "resolved"]);
                assert.strictEqual(list.filterSummary, "Aberta, Resolvida");
                assert.strictEqual(
                    list.activeFilterCount,
                    1,
                    "one state facet, not two filters"
                );
                assert.strictEqual(
                    target.querySelectorAll(".cc-state-option.is-active").length,
                    2
                );
                await click(target, ".cc-state-option--open");
                await click(target, ".cc-state-option--resolved");
                assert.deepEqual(store.state.filters.states, []);
                assert.strictEqual(list.filterSummary, "Todos os estados");
                assert.notOk(target.querySelector(".cc-inbox-filter-toggle b"));
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "the flyout focuses on opening and restores its trigger on Escape",
        async (assert) => {
            const store = filterStore();
            try {
                const {list, target} = await mountList(store);
                const trigger = target.querySelector(".cc-inbox-filter-toggle");
                await click(trigger);
                assert.strictEqual(
                    document.activeElement,
                    target.querySelector(".cc-inbox-filter-panel")
                );
                const event = new KeyboardEvent("keydown", {
                    key: "Escape",
                    bubbles: true,
                    cancelable: true,
                });
                document.activeElement.dispatchEvent(event);
                await nextTick();
                assert.ok(event.defaultPrevented);
                assert.notOk(list.ui.filtersOpen);
                assert.notOk(target.querySelector(".cc-inbox-filter-panel"));
                assert.strictEqual(document.activeElement, trigger);
                await click(trigger);
                target
                    .querySelector(".cc-search-field input")
                    .dispatchEvent(new PointerEvent("pointerdown", {bubbles: true}));
                await nextTick();
                assert.notOk(
                    list.ui.filtersOpen,
                    "a pointer outside dismisses the flyout"
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "clearing facets preserves the search and reloads once without a delayed duplicate",
        async (assert) => {
            const store = filterStore();
            try {
                Object.assign(store.state.filters, {
                    states: ["open", "resolved"],
                    accountId: 2,
                    query: "Ana",
                    responsibility: "mine",
                    unreadOnly: true,
                    excludeMuted: true,
                    conversationType: "direct",
                    tagId: 3,
                    activityTiming: "today",
                });
                const calls = [];
                store.loadConversations = async (options) => {
                    calls.push(options);
                    return true;
                };
                store.searchTimer = browser.setTimeout(() => {
                    assert.ok(
                        false,
                        "the pending search is canceled after its query was applied"
                    );
                }, 1000);
                const {list, target} = await mountList(store);
                assert.strictEqual(list.activeFilterCount, 8);
                await click(target, ".cc-inbox-filter-toggle");
                await click(target, ".cc-inbox-filter-clear");
                assert.deepEqual(store.state.filters, {
                    states: [],
                    accountId: false,
                    query: "Ana",
                    responsibility: "all",
                    responsibleId: false,
                    unreadOnly: false,
                    excludeMuted: false,
                    conversationType: false,
                    tagId: false,
                    tagIds: [],
                    activityTiming: false,
                });
                assert.strictEqual(list.activeFilterCount, 0);
                assert.strictEqual(store.searchTimer, null);
                assert.deepEqual(calls, [{reset: true}]);
                assert.strictEqual(
                    target.querySelector(".cc-search-field input").value,
                    "Ana"
                );
                assert.strictEqual(
                    target.querySelectorAll(".cc-tag-filter-option input:checked")
                        .length,
                    0
                );
                assert.strictEqual(
                    target.querySelector(".cc-account-filter select").value,
                    ""
                );
                assert.strictEqual(
                    target.querySelector(".cc-activity-filter select").value,
                    ""
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "compact density keeps search and filter access without hidden tool rows",
        async (assert) => {
            const store = filterStore();
            try {
                const {list, target} = await mountList(store);
                await click(target, ".cc-inbox-filter-toggle");
                await click(target, ".cc-inbox-density-toggle");
                assert.strictEqual(store.state.inboxDensity, "compact");
                assert.notOk(list.ui.filtersOpen);
                assert.ok(target.querySelector(".cc-search-field input"));
                assert.ok(target.querySelector(".cc-inbox-filter-toggle"));
                assert.notOk(target.querySelector(".is-compact-collapsed"));
                await click(target, ".cc-inbox-filter-toggle");
                assert.ok(list.ui.filtersOpen);
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "responsible selector and tag checkboxes compose without conflicting scopes",
        async (assert) => {
            const store = filterStore();
            try {
                store.state.bootstrap.tags.push({id: 4, name: "Comercial"});
                const {list, target} = await mountList(store);
                await click(target, ".cc-inbox-filter-toggle");
                await store.setFilter("responsibility", "mine");
                const select = target.querySelector(".cc-responsible-filter select");
                select.value = "7";
                select.dispatchEvent(new Event("change", {bubbles: true}));
                await nextTick();
                assert.strictEqual(store.state.filters.responsibility, "all");
                assert.strictEqual(store.state.filters.responsibleId, 7);
                const tags = target.querySelectorAll(".cc-tag-filter-option input");
                await click(tags[0]);
                await click(tags[1]);
                assert.deepEqual(store.conversationFilters(), {
                    states: ["open"],
                    responsible_id: 7,
                    tag_ids: [3, 4],
                });
                assert.strictEqual(list.activeFilterCount, 3);
                await click(tags[0]);
                assert.deepEqual(store.state.filters.tagIds, [4]);
                await store.setFilter("responsibility", "unassigned");
                assert.strictEqual(store.state.filters.responsibleId, false);
                store.state.bootstrap.tags = [];
                await nextTick();
                assert.ok(target.textContent.includes("Nenhum marcador cadastrado"));
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "specific responsible and tag filters reject stale rows on pages and updates",
        async (assert) => {
            const store = filterStore();
            try {
                const original = {
                    channel_id: 20,
                    state: "open",
                    responsible: {id: 7},
                    tags: [{id: 3}],
                };
                const wrongPerson = {...original, channel_id: 21, responsible: {id: 6}};
                const wrongTags = {...original, channel_id: 22, tags: [{id: 4}]};
                Object.assign(store.state.filters, {responsibleId: 7, tagIds: [3, 8]});
                store.state.conversations = [original];
                store.state.selectedChannelId = 20;
                store.applyConversationPage(
                    {items: [wrongPerson, wrongTags], total: 0},
                    {reset: true, silent: true, previousConversation: original}
                );
                assert.deepEqual(
                    store.state.conversations,
                    [],
                    "a missing selected row is not preserved under facets"
                );
                await store.reconcileConversationSelection({
                    reset: true,
                    silent: true,
                    previousSelected: 20,
                });
                assert.strictEqual(store.state.selectedChannelId, false);
                // Inserted explicitly, as an opening flow does.
                store.replaceConversation(original, {insert: true});
                assert.strictEqual(
                    store.state.conversations.length,
                    1,
                    "OR tags match one selected tag"
                );
                store.state.selectedChannelId = 20;
                store.replaceConversation({...original, tags: [{id: 4}]});
                assert.deepEqual(store.state.conversations, []);
                assert.strictEqual(store.state.selectedChannelId, false);
                store.state.conversations = [wrongPerson];
                store.applyConversationPage(
                    {items: [original]},
                    {reset: false, silent: true}
                );
                assert.deepEqual(
                    store.state.conversations.map((item) => item.channel_id),
                    [20]
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "legacy tag filters migrate to the multi-tag RPC and clear consistently",
        async (assert) => {
            const store = filterStore();
            try {
                store.state.filters.tagId = 3;
                assert.deepEqual(store.conversationFilters().tag_ids, [3]);
                assert.notOk("tag_id" in store.conversationFilters());
                await store.setFilter("tagIds", [4, 4, "8", -1, null]);
                assert.deepEqual(store.state.filters.tagIds, [4, 8]);
                assert.strictEqual(store.state.filters.tagId, false);
                await store.clearConversationFilters();
                assert.deepEqual(store.selectedFilterTagIds, []);
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "start flyout uses eligible inboxes, current inbox and exclusive focus dismissal",
        async (assert) => {
            const store = filterStore();
            try {
                store.state.bootstrap.accounts.push({
                    id: 3,
                    name: "Desconectada",
                    can_start_conversation: false,
                });
                store.state.filters.accountId = 1;
                const {list, target} = await mountList(store);
                await click(target, ".cc-inbox-filter-toggle");
                await click(target, ".cc-start-conversation-toggle");
                assert.notOk(list.ui.filtersOpen);
                assert.ok(list.ui.startOpen);
                assert.strictEqual(
                    target.querySelectorAll(
                        ".cc-start-conversation-panel select option"
                    ).length,
                    3
                );
                assert.strictEqual(list.ui.startAccountId, 1);
                assert.strictEqual(
                    document.activeElement,
                    target.querySelector(".cc-start-field input")
                );
                assert.ok(list.startPhoneHint.includes("Brasil (+55)"));
                document.activeElement.dispatchEvent(
                    new KeyboardEvent("keydown", {
                        key: "Escape",
                        bubbles: true,
                        cancelable: true,
                    })
                );
                await nextTick();
                assert.notOk(list.ui.startOpen);
                assert.strictEqual(
                    document.activeElement,
                    target.querySelector(".cc-start-conversation-toggle")
                );
                await click(target, ".cc-start-conversation-toggle");
                target
                    .querySelector(".cc-search-field input")
                    .dispatchEvent(new PointerEvent("pointerdown", {bubbles: true}));
                await nextTick();
                assert.notOk(list.ui.startOpen);
                await click(target, ".cc-start-conversation-toggle");
                await click(target, ".cc-inbox-filter-toggle");
                assert.notOk(list.ui.startOpen);
                assert.ok(list.ui.filtersOpen);
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "phone preview preserves raw input and discards stale inbox results",
        async (assert) => {
            const store = filterStore();
            try {
                store.state.filters.accountId = 1;
                const pending = [];
                store.normalizeStartPhone = (accountId, phone) =>
                    new Promise((resolve) => pending.push({accountId, phone, resolve}));
                const {list, target} = await mountList(store);
                await click(target, ".cc-start-conversation-toggle");
                const raw = "(11) 91234-5678";
                list.onStartPhoneInput({target: {value: raw}});
                const first = list.previewStartPhone();
                assert.notOk(list.canSubmitStart);
                list.onStartAccountChange({target: {value: "2"}});
                const second = list.previewStartPhone();
                pending[0].resolve({
                    normalized_phone: "5511912345678",
                    formatted_phone: "+55 11 91234-5678",
                });
                await first;
                assert.notOk(list.ui.startPreview);
                assert.ok(
                    list.ui.startPreviewPending,
                    "old response cannot clear the new request's busy state"
                );
                pending[1].resolve({
                    normalized_phone: "5511912345678",
                    formatted_phone: "+55 11 91234-5678",
                });
                await second;
                assert.ok(list.canSubmitStart);
                assert.strictEqual(list.ui.startPhone, raw);
                assert.deepEqual(
                    pending.map(({accountId, phone}) => [accountId, phone]),
                    [
                        [1, raw],
                        [2, raw],
                    ]
                );
                list.onStartPhoneInput({target: {value: raw + "x"}});
                assert.notOk(
                    list.canSubmitStart,
                    "a changed raw number requires fresh validation"
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "start stays open through Escape, outside click and cancel until the server responds",
        async (assert) => {
            const store = filterStore();
            try {
                store.state.filters.accountId = 1;
                store.normalizeStartPhone = async () => ({
                    normalized_phone: "5511912345678",
                    formatted_phone: "+55 11 91234-5678",
                });
                let resolveStart = null;
                let remainsCurrent = null;
                store.startConversation = (_account, _phone, {isCurrent}) => {
                    remainsCurrent = isCurrent;
                    return new Promise((resolve) => {
                        resolveStart = resolve;
                    });
                };
                const {list, target} = await mountList(store);
                await click(target, ".cc-start-conversation-toggle");
                list.ui.startPhone = "11 91234-5678";
                await list.previewStartPhone();
                const request = list.submitStartConversation();
                await nextTick();
                list.closeStartConversation();
                list.onFilterOutsidePointerdown({target: document.body});
                list.onShortcut({
                    key: "Escape",
                    preventDefault: () => undefined,
                    stopPropagation: () => undefined,
                });
                list.toggleFilters();
                assert.ok(list.ui.startOpen);
                assert.ok(
                    remainsCurrent(),
                    "the result cannot be discarded by dismissing the popover"
                );
                assert.ok(
                    target.querySelector('[aria-label="Fechar iniciar conversa"]')
                        .disabled
                );
                assert.notOk(list.ui.filtersOpen);
                resolveStart({channel_id: 22});
                assert.ok(await request);
                assert.notOk(list.ui.startPending);
                assert.notOk(list.ui.startOpen);
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "start errors remain inline and double submission creates only one request",
        async (assert) => {
            const store = filterStore();
            try {
                store.state.filters.accountId = 1;
                store.normalizeStartPhone = async () => ({
                    normalized_phone: "5511912345678",
                    formatted_phone: "+55 11 91234-5678",
                });
                let rejectStart = null;
                let writes = 0;
                store.startConversation = () => {
                    writes += 1;
                    return new Promise((_resolve, reject) => {
                        rejectStart = reject;
                    });
                };
                const {list, target} = await mountList(store);
                await click(target, ".cc-start-conversation-toggle");
                list.ui.startPhone = "11 91234-5678";
                await list.previewStartPhone();
                const request = list.submitStartConversation();
                assert.notOk(await list.submitStartConversation());
                assert.strictEqual(writes, 1);
                rejectStart({data: {message: "Número não encontrado no WhatsApp."}});
                await request;
                await nextTick();
                assert.ok(
                    target
                        .querySelector(".cc-start-error")
                        .textContent.includes("Número não encontrado")
                );
                assert.ok(list.ui.startOpen);
                assert.ok(list.canSubmitStart, "the agent can retry after an error");
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "an older start answer never undoes a newer preference of the channel",
        async (assert) => {
            const store = filterStore();
            try {
                const newer = {pinned: false, muted: false, revision: 4};
                store.state.conversations = [
                    {
                        channel_id: 22,
                        state: "open",
                        account: {id: 1},
                        preference: newer,
                    },
                ];
                store.call = async () => ({
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 22,
                    created: false,
                    normalized_phone: "5511912345678",
                    item: {
                        channel_id: 22,
                        state: "open",
                        account: {id: 1},
                        preference: {pinned: false, muted: true, revision: 3},
                    },
                });
                store.selectConversation = async (id) => {
                    store.state.selectedChannelId = id;
                    return true;
                };
                // The follow-up list refresh fails.
                store.loadConversations = async () => false;
                assert.ok(await store.startConversation(1, "(11) 91234-5678"));
                assert.deepEqual(store.selectedConversation.preference, newer);
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "mark all read is offered only with unread, confirms the count and runs",
        async (assert) => {
            const store = filterStore();
            try {
                store.state.conversations = [
                    {
                        channel_id: 1,
                        state: "open",
                        account: {id: 1},
                        unread_count: 0,
                        tags: [],
                    },
                ];
                const prepared = {
                    targets: [
                        {channel_id: 2, message_id: 20, max_message_id: 20},
                        {channel_id: 3, message_id: 30, max_message_id: 30},
                    ],
                    count: 2,
                    remaining: true,
                };
                const runs = [];
                const notes = [];
                let answer = {marked: 2, remaining: true};
                store.prepareMarkAllRead = async () => prepared;
                store.markAllRead = async (value) => {
                    runs.push(value);
                    return answer;
                };
                store.notify = (message) => notes.push(message);
                const dialogs = [];
                registry.category("services").add("hotkey", hotkeyService);
                registry.category("services").add("ui", uiService);
                registry.category("services").add("dialog", {
                    start: () => ({
                        add: (_component, props, options) => {
                            dialogs.push({props, options});
                            return () => undefined;
                        },
                    }),
                });
                makeFakeLocalizationService();
                const env = await makeTestEnv();
                const target = getFixture();
                const list = await mount(ConversationList, target, {
                    env,
                    props: {state: store.state, store},
                });
                const button = target.querySelector(".o_contact_center_mark_all_read");
                assert.ok(button.disabled, "nothing unread");
                store.state.conversations = [
                    ...store.state.conversations,
                    {
                        channel_id: 2,
                        state: "open",
                        account: {id: 1},
                        unread_count: 2,
                        tags: [],
                    },
                ];
                await nextTick();
                assert.notOk(button.disabled);
                assert.ok(await list.markAllRead());
                assert.ok(
                    dialogs[0].props.body.startsWith("Marcar 2 conversas como lidas?")
                );
                assert.ok(
                    dialogs[0].props.body.includes("não recebe confirmação de leitura")
                );
                // A second click while it is open opens nothing (L08-CLI-01).
                assert.notOk(await list.markAllRead());
                assert.strictEqual(dialogs.length, 1);
                assert.ok(await dialogs[0].props.confirm());
                dialogs[0].options.onClose();
                assert.deepEqual(runs, [prepared]);
                assert.ok(notes.includes("2 conversas marcadas como lidas."));
                // Closed by any path, the action is available again.
                answer = {marked: 0, remaining: false};
                assert.ok(await list.markAllRead());
                dialogs[1].options.onClose();
                assert.ok(await list.markAllRead(), "closing with X frees the action");
                assert.ok(await dialogs[2].props.confirm());
                assert.ok(notes.includes("Nenhuma conversa precisou ser marcada."));
                // An unknown outcome is reported as such; the reload claim depends
                // on the reload (L08-R09).
                let reloaded = true;
                store.markAllRead = async () => {
                    throw Object.assign(new Error("connection lost"), {
                        bulkReadUncertain: true,
                        listReloaded: reloaded,
                    });
                };
                assert.ok(await list.markAllRead());
                assert.notOk(await dialogs[3].props.confirm());
                assert.ok(
                    notes.includes(
                        "Não foi possível confirmar se as conversas foram marcadas " +
                            "como lidas. A lista foi recarregada."
                    )
                );
                reloaded = false;
                dialogs[3].options.onClose();
                assert.ok(await list.markAllRead());
                assert.notOk(await dialogs[4].props.confirm());
                assert.ok(
                    notes.includes(
                        "Não foi possível confirmar se as conversas foram marcadas " +
                            "como lidas. A lista será atualizada quando a conexão voltar."
                    )
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "store checks recording before writes and reuses an existing channel without changing it",
        async (assert) => {
            const store = filterStore();
            try {
                const item = {
                    channel_id: 22,
                    state: "resolved",
                    account: {id: 1},
                    responsible: {id: 7},
                    tags: [{id: 3}],
                };
                const payload = {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 22,
                    created: false,
                    normalized_phone: "5511912345678",
                    item,
                };
                const calls = [];
                store.call = async (method, args) => {
                    calls.push([method, args]);
                    return payload;
                };
                store.selectConversation = async (id) => {
                    store.state.selectedChannelId = id;
                    return true;
                };
                const reloads = [];
                store.loadConversations = async (options) => {
                    reloads.push(options);
                    store.applyConversationPage(
                        {
                            items: [{channel_id: 23, state: "open", account: {id: 1}}],
                            total: 50,
                            has_more: true,
                            next_cursor: "next",
                        },
                        {
                            reset: true,
                            silent: true,
                            previousConversation: store.selectedConversation,
                        }
                    );
                    return true;
                };
                store.registerConversationSelectionGuard(() => false);
                await assert.rejects(
                    store.startConversation(1, "(11) 91234-5678"),
                    /gravação/
                );
                assert.deepEqual(calls, []);
                store.registerConversationSelectionGuard(() => true);
                Object.assign(store.state.filters, {
                    states: ["open"],
                    accountId: 2,
                    query: "Ana",
                    responsibility: "mine",
                    responsibleId: 6,
                    tagIds: [8],
                    unreadOnly: true,
                    conversationType: "group",
                    activityTiming: "today",
                });
                assert.ok(await store.startConversation(1, "(11) 91234-5678"));
                assert.deepEqual(
                    calls,
                    [["start_conversation", [1, "(11) 91234-5678"]]],
                    "no send/update/assign RPC"
                );
                assert.deepEqual(store.conversationFilters(), {account_id: 1});
                assert.strictEqual(store.state.selectedChannelId, 22);
                assert.strictEqual(store.selectedConversation.state, "resolved");
                assert.deepEqual(store.selectedConversation.responsible, {id: 7});
                assert.deepEqual(store.selectedConversation.tags, [{id: 3}]);
                assert.deepEqual(reloads, [{reset: true, silent: true}]);
                assert.strictEqual(
                    store.state.conversationTotal,
                    50,
                    "real server count replaces the provisional row"
                );
                assert.ok(store.state.conversationsHaveMore);
                assert.deepEqual(
                    store.state.conversations.map((row) => row.channel_id),
                    [23, 22],
                    "existing channel remains visible beyond the first page"
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "closing the start session prevents a delayed response from navigating",
        async (assert) => {
            const store = filterStore();
            try {
                let resolve = null;
                let current = true;
                store.call = () =>
                    new Promise((done) => {
                        resolve = done;
                    });
                const request = store.startConversation(1, "11912345678", {
                    isCurrent: () => current,
                });
                assert.notOk(
                    await store.startConversation(1, "11912345678"),
                    "store also locks simultaneous writes"
                );
                current = false;
                resolve({
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 22,
                    item: {channel_id: 22, state: "open", account: {id: 1}},
                });
                assert.notOk(await request);
                assert.strictEqual(store.state.selectedChannelId, false);
                assert.deepEqual(store.state.conversations, []);
                assert.notOk(store.startConversationPending);
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "composer's start preflight blocks recording without preparing a draft switch",
        (assert) => {
            const composer = {switchHasBlockingWork: true, switchPrepared: true};
            const check = () =>
                MessageComposer.prototype.prepareConversationSwitch.call(
                    composer,
                    false,
                    {checkOnly: true}
                );
            assert.notOk(
                check(),
                "a missing target ID and an earlier prepared switch must not bypass capture protection"
            );
            composer.switchHasBlockingWork = false;
            composer.switchPrepared = false;
            assert.ok(check());
            assert.strictEqual(
                composer.switchPrepared,
                false,
                "preflight never persists or prepares the draft"
            );
        }
    );

    QUnit.test(
        "recording started during the RPC prevents subsequent navigation and filter changes",
        async (assert) => {
            const store = filterStore();
            try {
                let checks = 0;
                store.registerConversationSelectionGuard((_id, options) => {
                    assert.deepEqual(options, {checkOnly: true});
                    checks += 1;
                    return checks === 1;
                });
                store.call = async () => ({
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 22,
                    item: {channel_id: 22, state: "open", account: {id: 1}},
                });
                await assert.rejects(
                    store.startConversation(1, "11912345678"),
                    /Conclua a ação/
                );
                assert.strictEqual(checks, 2);
                assert.strictEqual(store.state.selectedChannelId, false);
                assert.deepEqual(store.conversationFilters(), {states: ["open"]});
                assert.notOk(store.startConversationPending);
            } finally {
                store.destroy();
            }
        }
    );
});
