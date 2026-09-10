/** @odoo-module **/

/* global QUnit */

import {click, getFixture, mount, nextTick} from "@web/../tests/helpers/utils";
import {ContactCenterApp} from "@contact_center_ui/js/contact_center_app.esm";
import {ContactCenterStore} from "@contact_center_ui/js/contact_center_store.esm";
import {ConversationList} from "@contact_center_ui/js/conversation_list.esm";
import {ConversationResolution} from "@contact_center_ui/js/conversation_resolution.esm";
import {SUPPORTED_SCHEMA_VERSION} from "@contact_center_ui/js/contact_center_model.esm";
import {dialogService} from "@web/core/dialog/dialog_service";
import {hotkeyService} from "@web/core/hotkeys/hotkey_service";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";
import {registry} from "@web/core/registry";
import {uiService} from "@web/core/ui/ui_service";

const TEST_REQUEST_ID = "00000000-0000-4000-8000-000000000001";

async function openResolutionDialog(store, doAction = () => undefined) {
    const services = registry.category("services");
    services.add("hotkey", hotkeyService);
    services.add("ui", uiService);
    services.add("dialog", dialogService);
    services.add("action", {start: () => ({doAction})});
    const env = await makeTestEnv();
    const target = getFixture();
    const container = registry.category("main_components").get("DialogContainer");
    await mount(container.Component, target, {env, props: container.props});
    env.services.dialog.add(ConversationResolution, {
        store,
        conversation: {channel_id: 10, name: "Contato de teste"},
    });
    await nextTick();
    return {env, target};
}

async function setDialogField(target, selector, value, eventType = "input") {
    const input = target.querySelector(selector);
    input.value = value;
    input.dispatchEvent(new Event(eventType, {bubbles: true}));
    await nextTick();
}

function dialog(overrides = {}) {
    return Object.assign(Object.create(ConversationResolution.prototype), {
        local: {
            loading: false,
            saving: false,
            items: [{id: 1, name: "Lead desqualificado"}],
            reasonId: "1",
            justification: "  Sem aderência ao serviço  ",
            revision: "revision-before",
            error: "",
        },
        requestId: "00000000-0000-4000-8000-000000000001",
        submittedValues: null,
        props: {conversation: {channel_id: 10}, store: {}, close: () => undefined},
        ...overrides,
    });
}

QUnit.module("contact_center_ui > conversation resolution", (hooks) => {
    hooks.beforeEach(() => {
        const banner = document.getElementById("oe_neutralize_banner");
        if (banner) {
            (banner.parentElement || banner).remove();
        }
    });

    QUnit.test(
        "resolve opens the wizard without changing the conversation",
        async (assert) => {
            let modal = null;
            const app = {
                ui: {stateChanging: false},
                resolutionAction: {target: "resolved"},
                selectedConversation: {channel_id: 10},
                store: {
                    setConversationState: () =>
                        assert.ok(false, "no direct resolution"),
                },
                addDialog: (Component, props, options) => {
                    assert.strictEqual(Component, ConversationResolution);
                    modal = {props, options};
                },
            };
            assert.ok(await ContactCenterApp.prototype.applyResolutionAction.call(app));
            assert.strictEqual(modal.props.conversation.channel_id, 10);
            assert.ok(app.ui.stateChanging);
            assert.notOk(
                await ContactCenterApp.prototype.applyResolutionAction.call(app)
            );
            modal.options.onClose();
            assert.notOk(app.ui.stateChanging);
        }
    );

    QUnit.test(
        "manual reopening keeps the existing explicit action",
        async (assert) => {
            const app = {
                ui: {stateChanging: false},
                resolutionAction: {target: "open"},
                store: {
                    setConversationState: async (state) => {
                        assert.strictEqual(state, "open");
                        return true;
                    },
                },
                addDialog: () => assert.ok(false, "no resolution wizard for reopening"),
            };
            assert.ok(await ContactCenterApp.prototype.applyResolutionAction.call(app));
            assert.notOk(app.ui.stateChanging);
        }
    );

    QUnit.test(
        "resolution requires a registered reason and a bounded justification",
        (assert) => {
            const component = dialog();
            assert.ok(component.canConfirm);
            for (const reason of ["", "99", "invalid"]) {
                component.local.reasonId = reason;
                assert.notOk(component.canConfirm);
            }
            component.local.reasonId = "1";
            for (const justification of ["", "   ", "x".repeat(501)]) {
                component.local.justification = justification;
                assert.notOk(component.canConfirm);
            }
            component.local.justification = "x".repeat(500);
            assert.ok(component.canConfirm);
            component.local.saving = true;
            assert.notOk(component.canConfirm);
        }
    );

    QUnit.test(
        "uncertain retries reuse the original decision and revision",
        async (assert) => {
            const component = dialog();
            const calls = [];
            component.props.store.resolveConversation = async (channelId, values) => {
                calls.push([channelId, {...values}]);
                if (calls.length === 1) {
                    throw new Error("Conexão interrompida");
                }
            };
            component.props.close = () => assert.step("closed");
            assert.notOk(await component.confirm());
            assert.strictEqual(component.local.error, "Conexão interrompida");
            component.local.justification = "changed in flight";
            component.local.revision = "new-revision";
            assert.ok(await component.confirm());
            assert.deepEqual(calls[0], [
                10,
                {
                    reasonId: 1,
                    justification: "Sem aderência ao serviço",
                    requestId: component.requestId,
                    revision: "revision-before",
                },
            ]);
            assert.deepEqual(calls[1], calls[0]);
            assert.verifySteps(["closed"]);
        }
    );

    QUnit.test(
        "catalog management is separate and cannot run after submission",
        async (assert) => {
            const component = dialog();
            component.action = {
                doAction: async (action, options) => {
                    assert.strictEqual(
                        action,
                        "contact_center_base.action_contact_center_resolution_reasons"
                    );
                    assert.step("manage");
                    options.onClose();
                },
            };
            component.loadCatalog = () => assert.step("reload");
            await component.manageReasons();
            assert.verifySteps([]);
            component.local.canManage = true;
            await component.manageReasons();
            assert.verifySteps(["manage", "reload"]);
            component.submittedValues = {};
            await component.manageReasons();
            assert.verifySteps([]);
        }
    );

    QUnit.test("a destroyed wizard ignores a late catalog response", async (assert) => {
        const component = dialog();
        component.props.store.call = async (method, args) => {
            assert.strictEqual(method, "resolution_reason_catalog");
            assert.deepEqual(args, [10]);
            component.destroyed = true;
            return {items: [], revision: "changed", can_manage: true};
        };
        await component.loadCatalog();
        assert.strictEqual(component.local.revision, "revision-before");
        assert.strictEqual(component.local.items.length, 1);
    });

    QUnit.test(
        "resolution response cannot update a different conversation",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            const values = dialog().submittedValues || {
                reasonId: 1,
                justification: "Motivo",
                requestId: "uuid",
                revision: "revision",
            };
            store.call = async (method, args) => {
                assert.strictEqual(method, "resolve_conversation");
                assert.deepEqual(args, [10, 1, "Motivo", "uuid", "revision"]);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    item: {channel_id: 20, state: "resolved"},
                };
            };
            await assert.rejects(store.resolveConversation(10, values), TypeError);
            assert.deepEqual(store.state.conversations, []);
            store.state.conversations = [{channel_id: 10, state: "open"}];
            store.call = async () => ({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                removed_from_conversation: true,
            });
            await assert.rejects(
                store.resolveConversation(10, values),
                TypeError,
                "resolution requires its scoped item, not a generic removal response"
            );
            assert.deepEqual(store.state.conversations, [
                {channel_id: 10, state: "open"},
            ]);
        }
    );

    QUnit.test(
        "mounted agent dialog requires reason and justification but cancellation never resolves",
        async (assert) => {
            const calls = [];
            const store = {
                operationCrypto: {randomUUID: () => TEST_REQUEST_ID},
                call: async (method, args) => {
                    calls.push([method, args]);
                    return {
                        items: [{id: 1, name: "Lead desqualificado"}],
                        revision: "revision-before",
                        can_manage: false,
                    };
                },
                resolveConversation: () =>
                    assert.ok(false, "cancel does not submit a resolution"),
            };
            const {env, target} = await openResolutionDialog(store);
            try {
                assert.containsOnce(target, ".o_dialog .cc-resolution-dialog");
                assert.containsNone(target, ".cc-resolution-dialog__manage");
                const list = {
                    ui: {filtersOpen: false},
                    navigateConversation: () =>
                        assert.ok(
                            false,
                            "modal keys cannot navigate the background list"
                        ),
                    searchRef: {
                        el: {
                            focus: () =>
                                assert.ok(
                                    false,
                                    "modal keys cannot focus the background search"
                                ),
                        },
                    },
                };
                const cancelButton = target.querySelector(
                    ".cc-resolution-dialog footer .btn-secondary"
                );
                for (const shortcut of [{key: "ArrowDown", altKey: true}, {key: "/"}]) {
                    assert.strictEqual(
                        ConversationList.prototype.onShortcut.call(list, {
                            ...shortcut,
                            target: cancelButton,
                            preventDefault: () =>
                                assert.ok(
                                    false,
                                    "the modal retains ownership of its keys"
                                ),
                        }),
                        false
                    );
                }
                assert.strictEqual(
                    target.querySelector("#cc-resolution-reason").required,
                    true
                );
                assert.strictEqual(
                    target.querySelector("#cc-resolution-justification").required,
                    true
                );
                assert.strictEqual(
                    target.querySelector("#cc-resolution-justification").maxLength,
                    500
                );
                const confirm = target.querySelector(".cc-resolution-dialog__confirm");
                assert.ok(confirm.disabled, "empty required fields prevent resolution");
                await setDialogField(target, "#cc-resolution-reason", "1", "change");
                assert.ok(
                    confirm.disabled,
                    "the registered reason alone is insufficient"
                );
                await setDialogField(target, "#cc-resolution-justification", "   ");
                assert.ok(confirm.disabled, "whitespace is not a justification");
                await setDialogField(
                    target,
                    "#cc-resolution-justification",
                    "Sem aderência ao serviço"
                );
                assert.notOk(confirm.disabled);
                assert.ok(confirm.textContent.includes("Resolver e registrar nota"));
                await click(target, ".cc-resolution-dialog footer .btn-secondary");
                assert.containsNone(target, ".o_dialog");
                assert.deepEqual(calls, [["resolution_reason_catalog", [10]]]);
            } finally {
                env.services.dialog.closeAll();
                await nextTick();
            }
        }
    );

    QUnit.test(
        "mounted supervisor dialog manages reasons separately and submits one decision with UUID and revision",
        async (assert) => {
            let catalogCalls = 0;
            let finishResolution = null;
            const pendingResolution = new Promise((resolve) => {
                finishResolution = resolve;
            });
            const decisions = [];
            const actions = [];
            const store = {
                operationCrypto: {randomUUID: () => TEST_REQUEST_ID},
                call: async (method, args) => {
                    assert.strictEqual(method, "resolution_reason_catalog");
                    assert.deepEqual(args, [10]);
                    catalogCalls++;
                    return {
                        items: [{id: 1, name: "Lead desqualificado"}],
                        revision: `revision-${catalogCalls}`,
                        can_manage: true,
                    };
                },
                resolveConversation: (channelId, values) => {
                    decisions.push([channelId, {...values}]);
                    return pendingResolution;
                },
            };
            const {env, target} = await openResolutionDialog(
                store,
                async (action, options) => {
                    actions.push(action);
                    options.onClose();
                }
            );
            try {
                assert.containsOnce(target, ".cc-resolution-dialog__manage");
                await click(target, ".cc-resolution-dialog__manage");
                assert.strictEqual(actions.length, 1);
                assert.strictEqual(
                    catalogCalls,
                    2,
                    "closing reason management refreshes the separate catalog"
                );
                assert.deepEqual(decisions, [], "reason maintenance is not resolution");
                await setDialogField(target, "#cc-resolution-reason", "1", "change");
                await setDialogField(
                    target,
                    "#cc-resolution-justification",
                    "  Não se enquadra no atendimento  "
                );
                await click(target, ".cc-resolution-dialog__confirm");
                assert.deepEqual(
                    decisions,
                    [
                        [
                            10,
                            {
                                reasonId: 1,
                                justification: "Não se enquadra no atendimento",
                                requestId: TEST_REQUEST_ID,
                                revision: "revision-1",
                            },
                        ],
                    ],
                    "catalog maintenance must not silently accept a newer conversation revision"
                );
                assert.ok(
                    target.querySelector(".cc-resolution-dialog__confirm").disabled
                );
                assert.ok(target.querySelector("#cc-resolution-reason").disabled);
                assert.ok(
                    target.querySelector("#cc-resolution-justification").disabled
                );
                assert.ok(
                    target.querySelector(".cc-resolution-dialog__manage").disabled
                );
                assert.ok(
                    target.querySelector(".cc-resolution-dialog footer .btn-secondary")
                        .disabled
                );
                finishResolution(true);
                await nextTick();
                assert.containsNone(
                    target,
                    ".o_dialog",
                    "the registered decision closes the wizard"
                );
                assert.strictEqual(decisions.length, 1);
            } finally {
                finishResolution(true);
                env.services.dialog.closeAll();
                await nextTick();
            }
        }
    );
});
