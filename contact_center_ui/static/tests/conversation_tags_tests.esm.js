/** @odoo-module **/

/* global QUnit */

import {
    ConversationTags,
    conversationTagSelected,
} from "@contact_center_ui/js/conversation_tags.esm";
import {click, getFixture, mount, nextTick} from "@web/../tests/helpers/utils";
import {MessageComposer} from "@contact_center_ui/js/message_composer.esm";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";
import {reactive} from "@odoo/owl";

QUnit.module("contact_center_ui > conversation tags and reply management", (hooks) => {
    hooks.beforeEach(() => {
        const banner = document.getElementById("oe_neutralize_banner");
        if (banner) {
            (banner.parentElement || banner).remove();
        }
    });

    QUnit.test(
        "conversation tag selection follows the current conversation",
        (assert) => {
            assert.notOk(conversationTagSelected(null, 1));
            assert.notOk(conversationTagSelected({}, 1));
            assert.ok(conversationTagSelected({tags: [{id: 1}]}, 1));
            assert.notOk(conversationTagSelected({tags: [{id: 2}]}, 1));
        }
    );

    QUnit.test(
        "registering a tag updates the catalog without changing any conversation",
        async (assert) => {
            let resolve = null;
            const pending = new Promise((done) => {
                resolve = done;
            });
            let attached = false;
            const component = {
                canCreateName: true,
                request: 1,
                local: {saving: false, loading: false, query: "Comercial"},
                props: {
                    conversation: {channel_id: 10},
                    store: {
                        state: {selectedChannelId: 10},
                        call: () => pending,
                        reconcileTagCatalog: () => assert.step("catalog"),
                        replaceConversation: () =>
                            assert.ok(
                                false,
                                "registration never changes conversation data"
                            ),
                        toggleTag: () => {
                            attached = true;
                        },
                    },
                },
                isSelected: () => false,
            };
            const creating = ConversationTags.prototype.createTag.call(component, {
                preventDefault: () => assert.step("submit"),
            });
            component.props.store.state.selectedChannelId = 20;
            resolve({
                items: [{id: 1, name: "Comercial", color: 0}],
                created_id: 1,
            });
            await creating;
            assert.notOk(
                attached,
                "Registration cannot apply a tag to the original or newly selected conversation"
            );
            assert.notOk(component.local.saving);
            assert.deepEqual(component.local.items, [
                {id: 1, name: "Comercial", color: 0},
            ]);
            assert.strictEqual(component.local.query, "");
            assert.strictEqual(
                component.local.notice,
                "Marcador cadastrado. Selecione-o para aplicar à conversa."
            );
            assert.deepEqual(component.props.conversation, {channel_id: 10});
            assert.verifySteps(["submit", "catalog"]);
        }
    );

    QUnit.test(
        "closing tag picker ignores an in-flight catalog response",
        async (assert) => {
            let resolve = null;
            const pending = new Promise((done) => {
                resolve = done;
            });
            const component = {
                request: 0,
                local: {open: false},
                props: {
                    conversation: {channel_id: 10},
                    store: {
                        call: () => pending,
                        reconcileTagCatalog: () => assert.step("reconcile"),
                    },
                },
            };
            const loading = ConversationTags.prototype.toggle.call(component);
            component.request++;
            resolve({items: [{id: 1, name: "Comercial", color: 0}], can_create: true});
            await loading;
            assert.deepEqual(component.local.items, []);
            assert.notOk(component.local.canCreate);
            assert.verifySteps([]);
        }
    );

    QUnit.test(
        "mounted tag picker loads the scoped catalog, searches and toggles with agent permissions",
        async (assert) => {
            const env = await makeTestEnv();
            const target = getFixture();
            const conversation = reactive({channel_id: 10, tags: []});
            const items = [
                {id: 1, name: "Comercial", color: 4},
                {id: 2, name: "Retorno", color: 7},
            ];
            const store = {
                call: async (method, args) => {
                    assert.strictEqual(method, "conversation_tag_catalog");
                    assert.deepEqual(args, [10]);
                    return {items, can_create: false};
                },
                reconcileTagCatalog: (catalog) => assert.deepEqual(catalog, items),
                toggleTag: async (id) => {
                    assert.strictEqual(id, 2);
                    conversation.tags = conversation.tags.length ? [] : [items[1]];
                    return true;
                },
            };
            await mount(ConversationTags, target, {
                env,
                props: {store, conversation},
            });
            const trigger = target.querySelector(".cc-conversation-tags__trigger");
            assert.notOk(target.querySelector(".cc-conversation-tags__panel"));
            assert.strictEqual(trigger.getAttribute("aria-haspopup"), "dialog");
            await click(trigger);
            const panel = target.querySelector(".cc-conversation-tags__panel");
            assert.strictEqual(panel.id, trigger.getAttribute("aria-controls"));
            assert.strictEqual(panel.getAttribute("role"), "dialog");
            assert.strictEqual(
                target.querySelectorAll(".cc-conversation-tags__items button").length,
                2
            );
            const input = target.querySelector(".cc-conversation-tags__search input");
            assert.strictEqual(document.activeElement, input);
            input.value = " RET ";
            input.dispatchEvent(new Event("input", {bubbles: true}));
            await nextTick();
            assert.strictEqual(
                target.querySelectorAll(".cc-conversation-tags__items button").length,
                1
            );
            assert.ok(
                target
                    .querySelector(".cc-conversation-tags__items button")
                    .textContent.includes("Retorno")
            );
            await click(target, ".cc-conversation-tags__items button");
            assert.deepEqual(conversation.tags, [items[1]]);
            assert.strictEqual(
                target
                    .querySelector(".cc-conversation-tags__items button")
                    .getAttribute("aria-pressed"),
                "true"
            );
            assert.strictEqual(
                target.querySelector(".cc-conversation-tags__count").textContent,
                "1"
            );
            await click(target, ".cc-conversation-tags__items button");
            assert.deepEqual(conversation.tags, []);
            input.value = "Novo marcador";
            input.dispatchEvent(new Event("input", {bubbles: true}));
            await nextTick();
            assert.notOk(
                target.querySelector(".cc-conversation-tags__create"),
                "an agent cannot create through the picker"
            );
            input.dispatchEvent(
                new KeyboardEvent("keydown", {key: "Escape", bubbles: true})
            );
            await nextTick();
            assert.notOk(target.querySelector(".cc-conversation-tags__panel"));
            assert.strictEqual(document.activeElement, trigger);
        }
    );

    QUnit.test(
        "a tag registered while the picker closes is reused later and applied only by explicit selection",
        async (assert) => {
            const env = await makeTestEnv();
            const target = getFixture();
            const conversation = reactive({channel_id: 10, tags: []});
            const created = {id: 3, name: "Retorno", color: 0};
            let resolveCreate = null;
            const pendingCreate = new Promise((resolve) => {
                resolveCreate = resolve;
            });
            const calls = [];
            let catalog = [];
            let selectedExplicitly = false;
            const store = {
                call: async (method, args) => {
                    calls.push([method, args]);
                    return method === "conversation_tag_catalog"
                        ? {items: catalog, can_create: true}
                        : pendingCreate;
                },
                reconcileTagCatalog: (items) => {
                    if (items.length) {
                        assert.deepEqual(items, [created]);
                    }
                    catalog = items;
                },
                replaceConversation: () =>
                    assert.ok(false, "registration never replaces the conversation"),
                toggleTag: async (id) => {
                    assert.ok(
                        selectedExplicitly,
                        "registration does not automatically apply the tag"
                    );
                    assert.strictEqual(id, created.id);
                    conversation.tags = [created];
                    return true;
                },
            };
            const component = await mount(ConversationTags, target, {
                env,
                props: {store, conversation},
            });
            await click(target, ".cc-conversation-tags__trigger");
            const input = target.querySelector(".cc-conversation-tags__search input");
            input.value = " Retorno ";
            input.dispatchEvent(new Event("input", {bubbles: true}));
            await nextTick();
            assert.ok(
                target
                    .querySelector(".cc-conversation-tags__create")
                    .textContent.includes("Cadastrar")
            );
            const creating = component.createTag({preventDefault: () => undefined});
            await nextTick();
            assert.ok(component.local.saving);
            assert.ok(target.querySelector(".cc-conversation-tags__create").disabled);
            await click(target, ".cc-conversation-tags__panel header button");
            assert.notOk(component.local.open);
            resolveCreate({
                items: [created],
                created_id: 3,
            });
            await creating;
            await nextTick();
            assert.deepEqual(calls, [
                ["conversation_tag_catalog", [10]],
                ["create_conversation_tag", [10, "Retorno"]],
            ]);
            assert.deepEqual(
                conversation.tags,
                [],
                "the original conversation remains unchanged"
            );
            assert.deepEqual(
                catalog,
                [created],
                "registration refreshes the reusable catalog"
            );
            assert.notOk(target.querySelector(".cc-conversation-tags__panel"));
            assert.notOk(target.querySelector(".cc-conversation-tags__count"));
            assert.notOk(component.local.saving);
            await click(target, ".cc-conversation-tags__trigger");
            const existingTag = target.querySelector(
                ".cc-conversation-tags__items button"
            );
            assert.ok(existingTag.textContent.includes("Retorno"));
            assert.strictEqual(existingTag.getAttribute("aria-pressed"), "false");
            assert.notOk(target.querySelector(".cc-conversation-tags__create"));
            selectedExplicitly = true;
            await click(existingTag);
            assert.deepEqual(
                conversation.tags,
                [created],
                "an explicit selection applies the existing registered tag"
            );
            assert.strictEqual(
                target.querySelector(".cc-conversation-tags__count").textContent,
                "1"
            );
        }
    );

    QUnit.test(
        "quick reply management opens a modal and cannot interrupt blocking composer work",
        (assert) => {
            const actions = [];
            const composer = {
                switchHasBlockingWork: true,
                action: {
                    doAction: (action) => {
                        actions.push(action);
                        return "opened";
                    },
                },
            };
            assert.strictEqual(
                MessageComposer.prototype.manageQuickReplies.call(composer),
                false
            );
            assert.deepEqual(
                actions,
                [],
                "recording or pending send work keeps the composer in place"
            );
            composer.switchHasBlockingWork = false;
            assert.strictEqual(
                MessageComposer.prototype.manageQuickReplies.call(composer),
                "opened"
            );
            assert.deepEqual(
                actions,
                [
                    {
                        type: "ir.actions.act_window",
                        name: "Respostas rápidas",
                        res_model: "contact.center.quick.reply.binding",
                        views: [
                            [false, "list"],
                            [false, "form"],
                        ],
                        target: "new",
                    },
                ],
                "the management dialog preserves the conversation and its draft"
            );
        }
    );
});
