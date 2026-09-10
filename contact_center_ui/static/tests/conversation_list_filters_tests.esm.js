/** @odoo-module **/

/* global QUnit */

import {click, getFixture, mount, nextTick} from "@web/../tests/helpers/utils";
import {ContactCenterStore} from "@contact_center_ui/js/contact_center_store.esm";
import {ConversationList} from "@contact_center_ui/js/conversation_list.esm";
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
            {id: 1, name: "Comercial"},
            {id: 2, name: "Atendimento"},
        ],
        tags: [{id: 3, name: "Retorno"}],
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
                assert.strictEqual(list.activeFilterCount, 7);
                await click(target, ".cc-inbox-filter-toggle");
                await click(target, ".cc-inbox-filter-clear");
                assert.deepEqual(store.state.filters, {
                    states: [],
                    accountId: false,
                    query: "Ana",
                    responsibility: "all",
                    unreadOnly: false,
                    conversationType: false,
                    tagId: false,
                    activityTiming: false,
                });
                assert.strictEqual(list.activeFilterCount, 0);
                assert.strictEqual(store.searchTimer, null);
                assert.deepEqual(calls, [{reset: true, selectFirst: true}]);
                assert.strictEqual(
                    target.querySelector(".cc-search-field input").value,
                    "Ana"
                );
                assert.strictEqual(
                    target.querySelector(".cc-tag-filter select").value,
                    ""
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
});
