/** @odoo-module **/

/* global QUnit */

import {
    BoundedImageQueue,
    DeferredImage,
} from "@contact_center_ui/js/deferred_image.esm";
import {
    CONNECTION_HEALTH_EVENT_TYPE,
    CONTACT_CENTER_NOTIFICATION_TYPE,
    SUPPORTED_SCHEMA_VERSION,
    connectionFleetMeta,
    connectionHealthDetail,
    connectionHealthNeedsAttention,
    connectionHealthStateMeta,
    contactCenterNotifications,
    conversationAvatarUrl,
    conversationDisplayName,
    conversationPreference,
    conversationResolutionAction,
    conversationResponsibility,
    conversationStateMeta,
    conversationUiPolicy,
    deliveryMeta,
    filterConversationsByResponsibility,
    formatFileSize,
    groupMetadataStateMeta,
    groupMetadataUi,
    groupRoleMeta,
    initials,
    isGroupConversation,
    isRenderableConversation,
    makeClientRequestId,
    mediaCapabilities,
    mediaCaptionAllowed,
    mergeConnectionHealth,
    mergeTimelineItems,
    messageActionEnabled,
    messageDispatchReasonMeta,
    messagePreviewText,
    messageStatusMeta,
    normalizeAttributionProjection,
    normalizeConnectionHealth,
    normalizeConversationGroup,
    normalizeGroupMetadata,
    normalizePartnerCompany,
    normalizeSearchText,
    partnerCompanyForIdentity,
    realtimeStatusMeta,
    sourceWebhookActionEnabled,
    validateEnvelope,
    validateMediaFile,
} from "@contact_center_ui/js/contact_center_model.esm";
import {
    ContactCenterStore,
    conversationFollowsCursor,
    formatOdooUtcDateTime,
    loadInboxDensityPreference,
    localDateTimeToOdooUtc,
    normalizeInboxActionParams,
    normalizeProductivity,
    normalizeQuickReplies,
    operationIntentFingerprint,
    operationJournalStorageKey,
    readOperationJournal,
    saveInboxDensityPreference,
    uploadMediaRequest,
    validateOperationEnvelope,
} from "@contact_center_ui/js/contact_center_store.esm";
import {
    ContactPanel,
    effectiveAgentsForConversation,
} from "@contact_center_ui/js/contact_panel.esm";
import {
    ConversationTimeline,
    groupDeliveryLabel,
    isControlTimelineMessage,
    isForwardedTimelineMessage,
    isMessageContinuation,
    messageAuthorLabel,
    timelineScrollDecision,
    timelineViewportNearBottom,
} from "@contact_center_ui/js/conversation_timeline.esm";
import {
    MAX_COMPOSER_ATTACHMENTS,
    MessageComposer,
    admitAttachmentSequence,
    buildAttachmentSendPlan,
    composerDraftStorageKey,
    composerHint,
    composerShortcut,
    localDateTimeInputValue,
    readComposerDrafts,
    scheduledMessageStateMeta,
    transferredFiles,
    writeComposerDrafts,
} from "@contact_center_ui/js/message_composer.esm";
import {
    newStructuredDraft,
    newStructuredRow,
    outboundStructuredCapabilities,
    safeStructuredUrl,
    structuredDraftSubmission,
    structuredMessageCard,
} from "@contact_center_ui/js/structured_content.esm";
import {
    MessageContent,
    formatAudioTime,
    isLocalMediaUrl,
    isViewablePdf,
    pdfJsViewerUrl,
    viewableMediaItems,
} from "@contact_center_ui/js/message_content.esm";
import {
    VoiceRecorderSession,
    formatVoiceRecordingDuration,
    selectVoiceRecordingFormat,
    voiceRecorderAvailable,
    voiceRecordingDescriptor,
    voiceRecordingErrorMessage,
} from "@contact_center_ui/js/voice_recorder.esm";
import {
    attributionOccurredAt,
    attributionProjectionVisible,
} from "@contact_center_ui/js/attribution_touchpoints.esm";
import {
    ConversationList,
    conversationGroupBadgeLabel,
    conversationInboxMetadata,
    conversationListShortcut,
    conversationPreviewText,
    groupConversationsByInbox,
} from "@contact_center_ui/js/conversation_list.esm";
import {
    contactCenterActivityAction,
    openContactCenterActivityGroup,
} from "@contact_center_ui/js/activity_group_view.esm";
import {BrowserAttention} from "@contact_center_ui/js/browser_attention.esm";
import {ConnectionHealth} from "@contact_center_ui/js/connection_health.esm";
import {conversationComposerAvailable} from "@contact_center_ui/js/contact_center_app.esm";
import {reactive} from "@odoo/owl";
import {hotkeyService} from "@web/core/hotkeys/hotkey_service";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";
import {makeFakeLocalizationService} from "@web/../tests/helpers/mock_services";
import {click, getFixture, mount, nextTick} from "@web/../tests/helpers/utils";
import {registry} from "@web/core/registry";
import {uiService} from "@web/core/ui/ui_service";

function canonicalGroup(overrides = {}) {
    return {
        display_name: "Equipe Solar",
        avatar_url: false,
        participant_count: 12,
        admin_count: 2,
        own_role: "member",
        metadata_state: "ready",
        last_synced_at: "2026-08-24 11:30:00",
        ...overrides,
    };
}

function attributionItem(publicRef, campaign, overrides = {}) {
    return {
        public_ref: publicRef,
        touchpoint_type: "paid_ad_click",
        evidence_level: "provider_asserted",
        network: "meta",
        source_platform: "instagram",
        source_type: "ad",
        entry_point_source: "ctwa_ad",
        entry_point_app: "instagram",
        utm_source: "",
        utm_medium: "",
        utm_campaign: campaign,
        creative_media_type: "image",
        show_ad_attribution: true,
        occurred_at: "2026-08-25 14:30:00",
        ...overrides,
    };
}

function attributionEnvelope(channelId, items, overrides = {}) {
    return {
        schema_version: SUPPORTED_SCHEMA_VERSION,
        channel_id: channelId,
        enabled: true,
        items,
        has_more: false,
        next_cursor: false,
        ...overrides,
    };
}

function conversationActivityAt(position) {
    return new Date(Date.UTC(2026, 7, 21, 12, 0, 0) - position * 10 * 1000)
        .toISOString()
        .slice(0, 19)
        .replace("T", " ");
}

function openConversation(overrides = {}) {
    return {state: "open", ...overrides};
}

function activityConversationCursor(channelId, lastActivityAt) {
    return {
        segment: "activity",
        channel_id: channelId,
        last_activity_at: lastActivityAt,
    };
}

function removeNeutralizedDatabaseBanner() {
    // The neutralized LAB injects this outside QUnit's fixture. Preserve the
    // DOM-leak assertion for addon elements while excluding host-owned UI.
    const banner = document.getElementById("oe_neutralize_banner");
    if (banner) {
        (banner.parentElement || banner).remove();
    }
}

QUnit.module("contact_center_ui > conversation lifecycle", (hooks) => {
    hooks.beforeEach(removeNeutralizedDatabaseBanner);

    function lifecycleStore() {
        const store = new ContactCenterStore({
            orm: {},
            busService: new EventTarget(),
            notification: false,
        });
        store.state.conversations = [
            openConversation({
                channel_id: 10,
                name: "Cliente",
                ignored: false,
                capabilities: {delete_conversation: true, ignore_conversation: true},
            }),
            openConversation({channel_id: 20}),
        ];
        store.state.selectedChannelId = 10;
        store.state.timelineChannelId = 10;
        store.state.messages = [{message_id: 100}];
        store.state.conversationTotal = 2;
        store.scheduleSynchronization = () => undefined;
        return store;
    }

    QUnit.test(
        "destructive conversation actions require effective server capabilities",
        async (assert) => {
            const store = lifecycleStore();
            store.state.bootstrap = {capabilities: {is_admin: true}};
            store.selectedConversation.capabilities = {
                delete_conversation: "true",
                ignore_conversation: false,
            };
            let calls = 0;
            store.call = async () => {
                calls++;
            };
            assert.notOk(await store.deleteConversation(10));
            assert.notOk(await store.setConversationIgnored(true, 10));
            assert.notOk(await store.deleteConversation(999));
            assert.strictEqual(
                calls,
                0,
                "no request without a true conversation capability"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "deletion tombstones defeat delayed lists and conversation responses",
        async (assert) => {
            const store = lifecycleStore();
            const oldConversation = {...store.selectedConversation};
            let finishList = null;
            store.call = (method) =>
                method === "list_conversations"
                    ? new Promise((resolve) => {
                          finishList = resolve;
                      })
                    : Promise.resolve({
                          schema_version: 1,
                          channel_id: 10,
                          removed_from_conversation: true,
                      });
            const listing = store.loadConversations({reset: true});
            assert.ok(await store.deleteConversation(10));
            assert.notOk(store.state.selectedChannelId);
            assert.deepEqual(store.state.messages, []);
            finishList({schema_version: 1, items: [oldConversation], total: 1});
            assert.notOk(
                await listing,
                "a list started before deletion is invalidated"
            );
            assert.notOk(
                store.replaceConversation(oldConversation),
                "a late mutation response cannot resurrect the row"
            );
            store.applyConversationPage(
                {items: [oldConversation]},
                {reset: true, silent: true, previousConversation: oldConversation}
            );
            assert.deepEqual(
                store.state.conversations,
                [],
                "a deleted previous selection is never preserved"
            );
            assert.notOk(await store.selectConversation(10));
            store.destroy();
        }
    );

    QUnit.test(
        "deletion notifications remove another session's selection without reselecting",
        (assert) => {
            const store = lifecycleStore();
            store.onNotification({
                detail: [
                    {
                        type: CONTACT_CENTER_NOTIFICATION_TYPE,
                        payload: {
                            schema_version: 1,
                            event_type: "conversation_deleted",
                            channel_id: 10,
                        },
                    },
                ],
            });
            assert.notOk(store.state.selectedChannelId);
            assert.deepEqual(
                store.state.conversations.map((item) => item.channel_id),
                [20]
            );
            assert.strictEqual(store.state.conversationTotal, 1);
            assert.notOk(store.replaceConversation(openConversation({channel_id: 10})));
            store.destroy();
        }
    );

    QUnit.test("deletion requires an exact confirmed tombstone", async (assert) => {
        const store = lifecycleStore();
        store.call = async () => ({
            schema_version: 1,
            channel_id: 20,
            removed_from_conversation: true,
        });
        assert.notOk(await store.deleteConversation(10));
        assert.strictEqual(store.state.selectedChannelId, 10);
        assert.strictEqual(store.state.messages.length, 1);
        assert.notOk(store.deletedConversationIds.has(10));
        store.destroy();
    });

    QUnit.test(
        "ignoring and resuming preserve the existing history and selected row",
        async (assert) => {
            const store = lifecycleStore();
            const calls = [];
            store.call = async (method, [channelId, ignored]) => {
                calls.push([method, channelId, ignored]);
                return {
                    schema_version: 1,
                    item: {...store.loadedConversation(channelId), ignored},
                };
            };
            assert.ok(await store.setConversationIgnored(true, 10));
            assert.ok(store.selectedConversation.ignored);
            assert.deepEqual(store.state.messages, [{message_id: 100}]);
            assert.ok(await store.setConversationIgnored(false, 10));
            assert.notOk(store.selectedConversation.ignored);
            assert.deepEqual(calls, [
                ["set_conversation_ignored", 10, true],
                ["set_conversation_ignored", 10, false],
            ]);
            store.destroy();
        }
    );

    QUnit.test(
        "mark unread drains in-flight seen writes and closes only its own selection",
        async (assert) => {
            const store = lifecycleStore();
            let finishSeen = null;
            const calls = [];
            store.call = (method) => {
                calls.push(method);
                if (method === "mark_seen") {
                    return new Promise((resolve) => {
                        finishSeen = resolve;
                    });
                }
                return Promise.resolve({
                    schema_version: 1,
                    item: {
                        ...store.loadedConversation(10),
                        unread_count: 1,
                        first_unread_message_id: 100,
                    },
                });
            };
            const seen = store.markSeen(100);
            const unread = store.markConversationUnread(10);
            await Promise.resolve();
            assert.deepEqual(
                calls,
                ["mark_seen"],
                "unread waits for the write already in flight"
            );
            assert.notOk(
                await store.markSeen(100),
                "viewport updates cannot start another seen request"
            );
            assert.notOk(
                await store.markConversationUnread(10),
                "repeated actions are ignored"
            );
            finishSeen({channel_id: 10, message_id: 100});
            await seen;
            assert.ok(await unread);
            assert.deepEqual(calls, ["mark_seen", "mark_conversation_unread"]);
            assert.notOk(store.state.selectedChannelId);
            assert.strictEqual(store.loadedConversation(10).unread_count, 1);
            assert.strictEqual(store.pendingSeenRequests.size, 0);
            assert.strictEqual(store.suspendedSeenChannels.size, 0);

            store.state.selectedChannelId = 20;
            assert.ok(await store.markConversationUnread(10));
            assert.strictEqual(
                store.state.selectedChannelId,
                20,
                "an unrelated conversation stays open"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "failed unread requests release the seen barrier without losing the chat",
        async (assert) => {
            const store = lifecycleStore();
            store.call = async () => {
                throw new Error("Temporary failure");
            };
            assert.notOk(await store.markConversationUnread(10));
            assert.strictEqual(store.state.selectedChannelId, 10);
            assert.strictEqual(store.suspendedSeenChannels.size, 0);
            store.call = async () => ({channel_id: 10, message_id: 100});
            assert.ok(await store.markSeen(100));
            store.destroy();
        }
    );

    QUnit.test(
        "conversation confirmations cancel safely and recheck permissions on confirm",
        async (assert) => {
            const store = lifecycleStore();
            let dialog = null;
            let calls = 0;
            store.call = async () => {
                calls++;
                return {
                    schema_version: 1,
                    channel_id: 10,
                    removed_from_conversation: true,
                };
            };
            const list = {
                store,
                ui: {pendingConversationIds: {}},
                addDialog: (_Component, props) => {
                    dialog = props;
                },
            };
            const confirm = (action) =>
                ConversationList.prototype.confirmConversationAction.call(
                    list,
                    store.selectedConversation,
                    action
                );
            assert.ok(confirm("delete"));
            assert.strictEqual(calls, 0, "opening a dialog cannot delete");
            assert.ok(dialog.body.includes("todas as suas mensagens"));
            dialog.cancel();
            assert.deepEqual(list.ui.pendingConversationIds, {});
            assert.ok(confirm("ignored"));
            assert.ok(
                dialog.body.includes(
                    "não serão registradas nesta caixa, para nenhum atendente"
                )
            );
            dialog.cancel();
            assert.ok(confirm("delete"));
            store.selectedConversation.capabilities.delete_conversation = false;
            assert.notOk(
                await dialog.confirm(),
                "revoked capabilities are checked again by the store"
            );
            assert.strictEqual(calls, 0);
            assert.deepEqual(list.ui.pendingConversationIds, {});
            store.destroy();
        }
    );
});

QUnit.module("contact_center_ui > model", (hooks) => {
    QUnit.test(
        "activity menu targets the inbox without changing native model groups",
        (assert) => {
            for (const [filter, timing] of [
                ["my", "due"],
                ["overdue", "overdue"],
                ["today", "today"],
                ["upcoming_all", "planned"],
                ["summary", "all"],
            ]) {
                assert.deepEqual(contactCenterActivityAction(filter), {
                    type: "ir.actions.client",
                    tag: "contact_center_ui.inbox",
                    name: "Contact Center",
                    params: {activity_timing: timing},
                });
            }
            const actions = [];
            const view = {
                activityGroup: {contactCenter: false},
                activityMenuViewOwner: {update: (values) => actions.push(values)},
                env: {services: {action: {doAction: (action) => actions.push(action)}}},
            };
            const target = document.createElement("button");
            target.dataset.filter = "today";
            const event = {target, stopPropagation: () => actions.push("stopped")};
            assert.notOk(openContactCenterActivityGroup(view, event));
            assert.deepEqual(actions, [], "Discuss and CRM keep their native handler");
            view.activityGroup.contactCenter = true;
            assert.ok(openContactCenterActivityGroup(view, event));
            assert.strictEqual(actions[2].params.activity_timing, "today");
            assert.deepEqual(actions[1], {isOpen: false});
        }
    );

    QUnit.test(
        "inbox action parameters reject unrecognized filters and channel IDs",
        (assert) => {
            assert.deepEqual(
                normalizeInboxActionParams({channel_id: 42, activity_timing: "due"}),
                {channelId: 42, activityTiming: "due"}
            );
            for (const channelId of [-1, 0, "42", true, {}, 1.5]) {
                assert.notOk(
                    normalizeInboxActionParams({channel_id: channelId}).channelId
                );
            }
            assert.deepEqual(normalizeInboxActionParams({activity_timing: "all"}), {
                channelId: false,
                activityTiming: "all",
            });
            assert.notOk(
                normalizeInboxActionParams({activity_timing: "unexpected"})
                    .activityTiming
            );
        }
    );

    QUnit.test(
        "activity entry filters all conversation states and does not open an arbitrary chat",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                initialActionParams: {activity_timing: "due"},
            });
            const loads = [];
            store.call = async () => ({
                schema_version: 1,
                capabilities: {followups: true},
            });
            store.loadConversations = async (options) => loads.push(options);
            await store.loadBootstrap();
            assert.deepEqual(loads, [{reset: true, selectFirst: false}]);
            assert.notOk(store.state.selectedChannelId);
            assert.deepEqual(store.state.filters.states, []);
            assert.strictEqual(store.conversationFilters().activity_timing, "due");
            assert.notOk(
                store.initialNavigation,
                "navigation intent is consumed only once"
            );
        }
    );

    QUnit.test(
        "direct activity links hydrate a channel outside the loaded page",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                initialActionParams: {channel_id: 42},
            });
            const calls = [];
            store.call = async (method, args) => {
                calls.push({method, args});
                return method === "bootstrap"
                    ? {schema_version: 1}
                    : {
                          schema_version: 1,
                          item: openConversation({
                              channel_id: 42,
                              name: "Exact channel",
                          }),
                      };
            };
            store.loadConversations = async () => {
                store.state.conversations = [openConversation({channel_id: 10})];
            };
            store.loadTimeline = async () => true;
            await store.loadBootstrap();
            assert.deepEqual(calls[1], {method: "get_conversation", args: [42]});
            assert.strictEqual(store.state.selectedChannelId, 42);
            assert.strictEqual(store.selectedConversation.name, "Exact channel");
            assert.strictEqual(store.state.mobilePane, "conversation");
        }
    );

    QUnit.test(
        "an inbox filter change during bootstrap cancels the initial channel intent",
        async (assert) => {
            const calls = [];
            let resolveList = () => false;
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                initialActionParams: {channel_id: 42},
            });
            store.call = async (method) => {
                calls.push(method);
                return {schema_version: 1};
            };
            store.loadConversations = () => {
                store.listRequest += 1;
                return new Promise((resolve) => {
                    resolveList = resolve;
                });
            };
            const loading = store.loadBootstrap();
            await Promise.resolve();
            store.listRequest += 1;
            resolveList(true);
            await loading;
            assert.deepEqual(calls, ["bootstrap"]);
            assert.notOk(store.state.selectedChannelId);
        }
    );

    QUnit.test(
        "a slow initial channel cannot replace a later selection or filter",
        async (assert) => {
            for (const change of ["selection", "filter", "destroy"]) {
                const store = new ContactCenterStore({
                    orm: {},
                    busService: {},
                    notification: false,
                });
                let resolve = () => false;
                store.call = () =>
                    new Promise((done) => {
                        resolve = done;
                    });
                const pending = store.openInitialConversation(42);
                if (change === "selection") {
                    store.state.selectedChannelId = 10;
                    store.timelineRequest += 1;
                }
                if (change === "filter") {
                    store.listRequest += 1;
                }
                if (change === "destroy") {
                    store.destroyed = true;
                }
                resolve({schema_version: 1, item: openConversation({channel_id: 42})});
                assert.notOk(await pending, change);
                assert.notOk(
                    store.state.conversations.some((item) => item.channel_id === 42),
                    change
                );
            }
        }
    );

    QUnit.test(
        "an inaccessible or mismatched initial channel does not select a fallback",
        async (assert) => {
            const notices = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.notify = (message) => notices.push(message);
            store.call = async () => ({
                schema_version: 1,
                item: openConversation({channel_id: 99}),
            });
            assert.notOk(await store.openInitialConversation(42));
            assert.notOk(store.state.selectedChannelId);
            assert.deepEqual(store.state.conversations, []);
            store.call = async () => {
                throw new Error("Denied");
            };
            assert.notOk(await store.openInitialConversation(42));
            assert.strictEqual(notices.length, 2);
        }
    );

    QUnit.test(
        "follow-up creation uses the conversation without a Kanban case",
        async (assert) => {
            const composer = Object.create(MessageComposer.prototype);
            const values = [];
            composer.local = {
                actionBusy: false,
                followupSummary: "Retornar",
                followupNote: "Contexto",
                followupDate: "2030-01-02",
                followupUserId: 7,
                followupActivityTypeId: 4,
            };
            composer.props = {
                store: {
                    scheduleFollowup: async (value) => {
                        values.push(value);
                        return true;
                    },
                },
            };
            assert.ok(await composer.createFollowup());
            assert.deepEqual(values, [
                {
                    summary: "Retornar",
                    note: "Contexto",
                    date_deadline: "2030-01-02",
                    user_id: 7,
                    activity_type_id: 4,
                },
            ]);
            assert.notOk(composer.local.actionBusy);
        }
    );
    hooks.beforeEach(removeNeutralizedDatabaseBanner);

    QUnit.test(
        "keeps inbox scope read-only and filters its effective responsible roster",
        async (assert) => {
            const agents = [
                {id: 7, name: "Ana"},
                {id: 8, name: "Bruno"},
                {id: 9, name: "Carla"},
            ];
            const teams = [
                {id: 3, name: "Comercial", agent_ids: [7, 9]},
                {id: 4, name: "Suporte", agent_ids: [8]},
            ];

            assert.deepEqual(
                effectiveAgentsForConversation(
                    {access_teams: [{id: 3}]},
                    teams,
                    agents
                ),
                [agents[0], agents[2]],
                "only agents from the inbox team can be selected"
            );
            assert.deepEqual(
                effectiveAgentsForConversation(
                    {access_teams: [{id: 99}]},
                    teams,
                    agents
                ),
                [],
                "an unknown team fails closed"
            );
            assert.deepEqual(
                effectiveAgentsForConversation({access_teams: []}, teams, agents),
                [],
                "a missing team never exposes the global agent roster"
            );
            assert.deepEqual(
                effectiveAgentsForConversation(
                    {access_users: [{id: 8}], access_teams: []},
                    teams,
                    agents
                ),
                [agents[1]],
                "a user-only inbox exposes only its access user"
            );
            assert.deepEqual(
                effectiveAgentsForConversation(
                    {access_users: [{id: 8}], access_teams: [{id: 3}]},
                    teams,
                    agents
                ),
                agents,
                "direct access and team membership are combined into one roster"
            );
            assert.deepEqual(
                effectiveAgentsForConversation(
                    {access_users: [{id: 7}], access_teams: [{id: 3}]},
                    teams,
                    agents
                ),
                [agents[0], agents[2]],
                "a directly allowed user already in the team is not duplicated"
            );
            assert.deepEqual(
                effectiveAgentsForConversation(
                    {access_users: [{id: 7}, {id: 8}], access_teams: []},
                    teams,
                    agents
                ),
                [agents[0], agents[1]],
                "multiple direct access users can be assigned without a team"
            );
            assert.deepEqual(
                effectiveAgentsForConversation(
                    {
                        access_users: [{id: 7}, {id: 8}],
                        access_teams: [{id: 3}, {id: 4}],
                    },
                    teams,
                    agents
                ),
                agents,
                "multiple users and teams grant the union without duplicate agents"
            );
            assert.deepEqual(
                effectiveAgentsForConversation(
                    {access_users: [{id: 8}], access_teams: [{id: 99}]},
                    teams,
                    agents
                ),
                [agents[1]],
                "an unknown team does not suppress an independent direct grant"
            );
            assert.deepEqual(
                effectiveAgentsForConversation(
                    {access_users: [{id: 7}], access_teams: [{id: 4}]},
                    teams,
                    agents
                ),
                [agents[0], agents[1]],
                "removing one team retains the remaining direct and team grants"
            );

            const patches = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.bootstrap = {capabilities: {manage_assignment: false}};
            store.updateConversation = async (patch) => {
                patches.push(patch);
                return true;
            };
            assert.notOk(
                await store.setResponsible("7"),
                "a revoked assignment capability blocks the RPC"
            );
            assert.deepEqual(patches, []);

            store.state.bootstrap.capabilities.manage_assignment = true;
            assert.ok(await store.setResponsible("7"));
            assert.ok(await store.setResponsible(""));
            assert.deepEqual(patches, [{responsible_id: 7}, {responsible_id: false}]);
        }
    );

    QUnit.test(
        "normalizes the linked company projection and fails closed",
        (assert) => {
            const company = normalizePartnerCompany({
                id: "17",
                name: "  Soloz Industrial  ",
                email: " contato@example.invalid ",
                phone: " +551532242610 ",
                vat: " 12345678000190 ",
                private_payload: {token: "must-not-leak"},
            });
            assert.deepEqual(company, {
                id: 17,
                name: "Soloz Industrial",
                email: "contato@example.invalid",
                phone: "+551532242610",
                vat: "12345678000190",
            });
            assert.deepEqual(partnerCompanyForIdentity({partner: {company}}), company);
            assert.notOk(normalizePartnerCompany({id: 17, name: "   "}));
            assert.notOk(normalizePartnerCompany({id: -1, name: "Invalid"}));
            assert.notOk(
                partnerCompanyForIdentity({partner: {company: {malformed: true}}})
            );
            assert.notOk(partnerCompanyForIdentity({partner: false}));
        }
    );

    QUnit.test(
        "searches and links a company only for an eligible contact",
        async (assert) => {
            let nextTimerId = 0;
            const scheduled = new Map();
            const companyTimer = {
                setTimeout(callback) {
                    const timerId = ++nextTimerId;
                    scheduled.set(timerId, callback);
                    return timerId;
                },
                clearTimeout(timerId) {
                    scheduled.delete(timerId);
                },
                async flush() {
                    const callbacks = [...scheduled.values()];
                    scheduled.clear();
                    await Promise.all(callbacks.map((callback) => callback()));
                },
            };
            const emptyIdentity = {
                id: 5,
                name: "Leonardo Hirata",
                partner: {
                    id: 11,
                    name: "Leonardo Hirata",
                    is_company: false,
                    company_linking_allowed: true,
                    company: false,
                },
            };
            const linkedIdentity = {
                ...emptyIdentity,
                partner: {
                    ...emptyIdentity.partner,
                    company_linking_allowed: false,
                    company: {
                        id: 17,
                        name: "Soloz Industrial",
                        email: "contato@example.invalid",
                        phone: "",
                        vat: "12345678000190",
                    },
                },
            };
            const calls = [];
            let resolveLink = null;
            const store = new ContactCenterStore({
                orm: {
                    async call(_model, method, args) {
                        calls.push({method, args});
                        if (method === "search_partner_companies") {
                            return {
                                schema_version: SUPPORTED_SCHEMA_VERSION,
                                items: [
                                    linkedIdentity.partner.company,
                                    {id: false, name: "Malformed"},
                                ],
                            };
                        }
                        if (method === "link_partner_company") {
                            return new Promise((resolve) => {
                                resolveLink = resolve;
                            });
                        }
                        throw new Error(`Unexpected method: ${method}`);
                    },
                },
                busService: {},
                notification: false,
                companyTimer,
            });
            store.state.bootstrap = {
                capabilities: {link_company: false, create_company: true},
            };
            store.state.selectedChannelId = 31;
            store.state.conversations = [
                {
                    channel_id: 31,
                    state: "open",
                    name: "Leonardo Hirata",
                    identity: emptyIdentity,
                },
            ];
            assert.notOk(
                store.openCompanyLinker("search"),
                "a revoked capability blocks the editor"
            );

            store.state.bootstrap.capabilities.link_company = true;
            assert.ok(store.openCompanyLinker("search"));
            assert.ok(store.setCompanySearch("So"));
            assert.ok(store.setCompanySearch("Soloz"));
            assert.notOk(
                store.openCompanyLinker("search"),
                "clicking the active mode does not reset its draft"
            );
            assert.strictEqual(store.state.companyLinker.query, "Soloz");
            assert.strictEqual(scheduled.size, 1, "the previous debounce is cancelled");
            await companyTimer.flush();
            assert.deepEqual(store.state.companyLinker.results, [
                linkedIdentity.partner.company,
            ]);
            assert.deepEqual(calls[0], {
                method: "search_partner_companies",
                args: [31, 11, "Soloz"],
            });

            assert.ok(store.setCompanySearch("Soloz Industrial"));
            assert.deepEqual(
                store.state.companyLinker.results,
                [],
                "a new query cannot leave stale companies clickable"
            );
            await companyTimer.flush();
            const linking = store.linkPartnerCompany(17);
            assert.strictEqual(store.state.companyLinker.phase, "saving");
            assert.notOk(
                store.setCompanySearch("Late query"),
                "saving cannot be replaced by another search"
            );
            resolveLink({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                company: linkedIdentity.partner.company,
                identity: linkedIdentity,
            });
            assert.ok(await linking);
            assert.deepEqual(calls[2], {
                method: "link_partner_company",
                args: [31, 11, 17],
            });
            assert.notOk(store.state.companyLinker.open);
            assert.strictEqual(
                store.state.conversations[0].identity.name,
                "Leonardo Hirata",
                "the company never replaces the person's display name"
            );
            assert.strictEqual(store.state.conversations[0].name, "Leonardo Hirata");
            assert.deepEqual(
                partnerCompanyForIdentity(store.state.conversations[0].identity),
                linkedIdentity.partner.company
            );
            assert.notOk(
                store.openCompanyLinker("search"),
                "an existing company makes the relation read-only"
            );
        }
    );

    QUnit.test(
        "creates a company once and ignores a stale editor response",
        async (assert) => {
            let resolveCreate = null;
            let createCalls = 0;
            const firstIdentity = {
                id: 5,
                name: "First Contact",
                partner: {
                    id: 11,
                    name: "First Contact",
                    is_company: false,
                    company_linking_allowed: true,
                    company: false,
                },
            };
            const secondIdentity = {
                id: 6,
                name: "Second Contact",
                partner: {
                    id: 12,
                    name: "Second Contact",
                    is_company: false,
                    company_linking_allowed: true,
                    company: false,
                },
            };
            const createdIdentity = {
                ...firstIdentity,
                partner: {
                    ...firstIdentity.partner,
                    company_linking_allowed: false,
                    company: {
                        id: 27,
                        name: "Created Company",
                        email: "",
                        phone: "",
                        vat: "",
                    },
                },
            };
            const store = new ContactCenterStore({
                orm: {
                    async call(_model, method) {
                        if (method !== "create_and_link_partner_company") {
                            throw new Error(`Unexpected method: ${method}`);
                        }
                        createCalls += 1;
                        return new Promise((resolve) => {
                            resolveCreate = resolve;
                        });
                    },
                },
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {
                capabilities: {link_company: true, create_company: true},
            };
            store.state.selectedChannelId = 31;
            store.state.conversations = [
                openConversation({channel_id: 31, identity: firstIdentity}),
                openConversation({channel_id: 32, identity: secondIdentity}),
            ];
            assert.ok(store.openCompanyLinker("create"));
            const firstSubmit = store.createAndLinkPartnerCompany({
                name: "Created Company",
            });
            assert.notOk(
                await store.createAndLinkPartnerCompany({name: "Duplicate"}),
                "saving blocks a duplicate submit"
            );
            assert.strictEqual(createCalls, 1);

            store.state.selectedChannelId = 32;
            store.closeCompanyLinker();
            assert.ok(store.openCompanyLinker("search"));
            resolveCreate({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                company: createdIdentity.partner.company,
                identity: createdIdentity,
            });
            assert.ok(await firstSubmit);
            assert.ok(store.state.companyLinker.open);
            assert.strictEqual(store.state.companyLinker.channelId, 32);
            assert.strictEqual(
                store.state.conversations.find((item) => item.channel_id === 31)
                    .identity.partner.company.id,
                27,
                "the original conversation still receives the committed identity"
            );
        }
    );

    QUnit.test(
        "keeps a stale company response away from a newly linked contact",
        async (assert) => {
            let resolveLink = null;
            const firstIdentity = {
                id: 5,
                name: "First Contact",
                partner: {
                    id: 11,
                    name: "First Contact",
                    is_company: false,
                    company_linking_allowed: true,
                    company: false,
                },
            };
            const linkedFirstIdentity = {
                ...firstIdentity,
                partner: {
                    ...firstIdentity.partner,
                    company_linking_allowed: false,
                    company: {
                        id: 17,
                        name: "First Company",
                        email: "",
                        phone: "",
                        vat: "",
                    },
                },
            };
            const secondIdentity = {
                id: 5,
                name: "Second Contact",
                partner: {
                    id: 12,
                    name: "Second Contact",
                    is_company: false,
                    company_linking_allowed: true,
                    company: false,
                },
            };
            const store = new ContactCenterStore({
                orm: {
                    async call(_model, method, args) {
                        assert.strictEqual(method, "link_partner_company");
                        assert.deepEqual(args, [31, 11, 17]);
                        return new Promise((resolve) => {
                            resolveLink = resolve;
                        });
                    },
                },
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {
                capabilities: {link_company: true, create_company: true},
            };
            store.state.selectedChannelId = 31;
            store.state.conversations = [
                openConversation({channel_id: 31, identity: firstIdentity}),
            ];

            assert.ok(store.openCompanyLinker("search"));
            assert.strictEqual(store.state.companyLinker.partnerId, 11);
            const revisionBeforeSave = store.state.companyOperationsRevision;
            const pendingLink = store.linkPartnerCompany(17);
            assert.strictEqual(
                store.state.companyOperationsRevision,
                revisionBeforeSave + 1
            );
            assert.ok(store.companyOperationPending(31, 11));
            assert.notOk(
                await store.unlinkPartner(),
                "the contact cannot be unlinked while its company write is pending"
            );

            store.replaceConversation(
                openConversation({channel_id: 31, identity: secondIdentity})
            );
            assert.notOk(store.state.companyLinker.open);
            assert.ok(store.openCompanyLinker("search"));
            assert.strictEqual(store.state.companyLinker.partnerId, 12);
            resolveLink({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                company: linkedFirstIdentity.partner.company,
                identity: linkedFirstIdentity,
            });
            assert.ok(await pendingLink);
            assert.strictEqual(store.state.conversations[0].identity.partner.id, 12);
            assert.ok(store.state.companyLinker.open);
            assert.strictEqual(store.state.companyLinker.partnerId, 12);
            assert.notOk(store.companyOperationPending(31, 11));
            assert.strictEqual(
                store.state.companyOperationsRevision,
                revisionBeforeSave + 2
            );
        }
    );

    QUnit.test(
        "reactively releases a company operation after leaving and returning",
        async (assert) => {
            let resolveLink = null;
            const firstIdentity = {
                id: 5,
                name: "First Contact",
                partner: {
                    id: 11,
                    name: "First Contact",
                    is_company: false,
                    company_linking_allowed: true,
                    company: false,
                },
            };
            const secondIdentity = {
                id: 6,
                name: "Second Contact",
                partner: {
                    id: 12,
                    name: "Second Contact",
                    is_company: false,
                    company_linking_allowed: true,
                    company: false,
                },
            };
            const store = new ContactCenterStore({
                orm: {
                    async call() {
                        return new Promise((resolve) => {
                            resolveLink = resolve;
                        });
                    },
                },
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {
                capabilities: {link_company: true, create_company: true},
            };
            store.state.conversations = [
                openConversation({channel_id: 31, identity: firstIdentity}),
                openConversation({channel_id: 32, identity: secondIdentity}),
            ];
            store.state.selectedChannelId = 31;
            assert.ok(store.openCompanyLinker("search"));
            const revisionBeforeSave = store.state.companyOperationsRevision;
            const pendingLink = store.linkPartnerCompany(17);

            store.state.selectedChannelId = 32;
            store.closeCompanyLinker();
            store.state.selectedChannelId = 31;
            assert.ok(store.companyOperationPending(31, 11));
            assert.strictEqual(
                store.state.companyOperationsRevision,
                revisionBeforeSave + 1
            );

            resolveLink({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                company: {id: 17, name: "Wrong response"},
                identity: {...firstIdentity, partner: {...firstIdentity.partner}},
            });
            assert.notOk(
                await pendingLink,
                "a malformed mutation response fails closed"
            );
            assert.notOk(store.companyOperationPending(31, 11));
            assert.strictEqual(
                store.state.companyOperationsRevision,
                revisionBeforeSave + 2,
                "settlement updates reactive state even with the editor closed"
            );
            assert.notOk(store.state.companyLinker.open);
            assert.strictEqual(
                store.state.conversations[0].identity.partner.company,
                false
            );
        }
    );

    QUnit.test(
        "keeps person first while searching and linking an explicit central company",
        async (assert) => {
            let nextTimerId = 0;
            const scheduled = new Map();
            const contactTimer = {
                setTimeout(callback) {
                    const timerId = ++nextTimerId;
                    scheduled.set(timerId, callback);
                    return timerId;
                },
                clearTimeout(timerId) {
                    scheduled.delete(timerId);
                },
                async flush() {
                    const callbacks = [...scheduled.values()];
                    scheduled.clear();
                    await Promise.all(callbacks.map((callback) => callback()));
                },
            };
            const linkedIdentity = {
                id: 5,
                name: "Soloz Central",
                persona_kind: "contact",
                link_kind: "central_company",
                link_invariant_valid: true,
                partner: {
                    id: 71,
                    name: "Soloz Central",
                    email: "central@example.invalid",
                    phone: "+551532242610",
                    vat: "12345678000190",
                    is_company: true,
                    company_linking_allowed: false,
                    company: false,
                },
            };
            const calls = [];
            const store = new ContactCenterStore({
                orm: {
                    async call(_model, method, args) {
                        calls.push({method, args});
                        if (method === "search_central_companies") {
                            return {
                                schema_version: SUPPORTED_SCHEMA_VERSION,
                                items: [
                                    {
                                        id: 71,
                                        name: " Soloz Central ",
                                        email: "central@example.invalid",
                                        phone: "+551532242610",
                                        vat: "12345678000190",
                                    },
                                    {id: false, name: "Malformed"},
                                ],
                            };
                        }
                        if (method === "link_central_company") {
                            return {
                                schema_version: SUPPORTED_SCHEMA_VERSION,
                                identity: linkedIdentity,
                            };
                        }
                        throw new Error(`Unexpected method: ${method}`);
                    },
                },
                busService: {},
                notification: false,
                contactTimer,
            });
            store.state.bootstrap = {
                capabilities: {
                    link_contact: true,
                    create_contact: true,
                    link_central_company: true,
                    create_central_company: true,
                },
            };
            store.state.selectedChannelId = 31;
            store.state.conversations = [
                {
                    channel_id: 31,
                    state: "open",
                    identity: {
                        id: 5,
                        name: "551532242610",
                        persona_kind: "guest",
                        partner: false,
                    },
                },
            ];

            assert.ok(
                store.openContactLinker("search", "person"),
                "the recommended flow opens as a person"
            );
            assert.strictEqual(store.state.contactLinker.targetKind, "person");
            assert.ok(store.openContactLinker("search", "central_company"));
            assert.strictEqual(store.state.contactLinker.targetKind, "central_company");
            assert.ok(store.setContactSearch("Soloz"));
            await contactTimer.flush();
            assert.deepEqual(calls[0], {
                method: "search_central_companies",
                args: [31, "Soloz"],
            });
            assert.deepEqual(store.state.contactLinker.results, [
                {
                    id: 71,
                    name: "Soloz Central",
                    email: "central@example.invalid",
                    phone: "+551532242610",
                    vat: "12345678000190",
                },
            ]);

            assert.ok(await store.linkCentralCompany(71));
            assert.deepEqual(calls[1], {
                method: "link_central_company",
                args: [31, 71],
            });
            assert.strictEqual(
                store.state.conversations[0].identity.partner.is_company,
                true
            );
            assert.notOk(store.state.contactLinker.open);
            assert.notOk(
                store.openContactLinker("search", "person"),
                "a linked company cannot be replaced from the guest editor"
            );
        }
    );

    QUnit.test(
        "creates a central company through its dedicated contract",
        async (assert) => {
            const calls = [];
            const createdIdentity = {
                id: 6,
                name: "Plantão Solar",
                persona_kind: "contact",
                link_kind: "central_company",
                link_invariant_valid: true,
                partner: {
                    id: 81,
                    name: "Plantão Solar",
                    email: "",
                    phone: "+5515999990000",
                    vat: "",
                    is_company: true,
                    company_linking_allowed: false,
                    company: false,
                },
            };
            const store = new ContactCenterStore({
                orm: {
                    async call(_model, method, args) {
                        calls.push({method, args});
                        return {
                            schema_version: SUPPORTED_SCHEMA_VERSION,
                            identity: createdIdentity,
                        };
                    },
                },
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {
                capabilities: {
                    link_contact: true,
                    create_contact: true,
                    link_central_company: true,
                    create_central_company: true,
                },
            };
            store.state.selectedChannelId = 32;
            store.state.conversations = [
                {
                    channel_id: 32,
                    state: "open",
                    identity: {
                        id: 6,
                        name: "5515999990000",
                        persona_kind: "guest",
                        partner: false,
                    },
                },
            ];

            assert.ok(store.openContactLinker("create", "central_company"));
            assert.ok(
                await store.createAndLinkCentralCompany({
                    name: "Plantão Solar",
                    phone: "+5515999990000",
                })
            );
            assert.deepEqual(calls, [
                {
                    method: "create_and_link_central_company",
                    args: [32, {name: "Plantão Solar", phone: "+5515999990000"}],
                },
            ]);
            assert.strictEqual(store.state.conversations[0].name, "Plantão Solar");
        }
    );

    QUnit.test(
        "contact search rows keep canonical IDs without shadowing the linked partner",
        async (assert) => {
            registry.category("services").add("action", {
                start: () => ({doAction: async () => undefined}),
            });
            makeFakeLocalizationService();
            const env = await makeTestEnv();
            const target = getFixture();
            const store = new ContactCenterStore({
                orm: {},
                busService: new EventTarget(),
                stateFactory: reactive,
                notification: false,
            });
            store.state.bootstrap = {
                capabilities: {link_contact: true, link_central_company: true},
            };
            store.state.selectedChannelId = 32;
            store.state.detailsOpen = true;
            store.state.conversations = [
                openConversation({
                    channel_id: 32,
                    name: "Perfil convidado",
                    identity: {
                        id: 7,
                        name: "Perfil convidado",
                        persona_kind: "guest",
                        link_kind: false,
                        partner: false,
                        aliases: [],
                    },
                }),
            ];
            const calls = [];
            store.linkPartner = async (id) => {
                calls.push({kind: "person", id});
                store.closeContactLinker();
            };
            store.linkCentralCompany = async (id) => {
                calls.push({kind: "central_company", id});
                store.closeContactLinker();
            };
            try {
                const panel = await mount(ContactPanel, target, {
                    env,
                    props: {
                        conversation: store.selectedConversation,
                        state: store.state,
                        store,
                    },
                });
                for (const targetKind of ["person", "central_company"]) {
                    assert.ok(store.openContactLinker("search", targetKind));
                    store.state.contactLinker.results = [
                        {id: 17, name: "Mesmo nome", phone: "1111", email: ""},
                        {id: 23, name: "Mesmo nome", phone: "2222", email: ""},
                    ];
                    store.state.contactLinker.phase = "ready";
                    await nextTick();
                    const rows = target.querySelectorAll(".cc-contact-results button");
                    assert.strictEqual(
                        rows.length,
                        2,
                        "same-name results are distinct records"
                    );
                    assert.strictEqual(
                        rows[0].querySelector("small").textContent,
                        "1111"
                    );
                    assert.strictEqual(
                        rows[1].querySelector("small").textContent,
                        "2222"
                    );
                    assert.notOk(
                        panel.partner,
                        "search results never replace the linked-partner getter"
                    );
                    await click(rows[1]);
                    assert.deepEqual(
                        calls[calls.length - 1],
                        {kind: targetKind, id: 23},
                        "the selected row sends its canonical record ID"
                    );
                    assert.notOk(store.state.contactLinker.open);
                }
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "opens only the currently linked partner and reports action failures",
        async (assert) => {
            const actions = [];
            const notifications = [];
            const panel = {
                partner: {id: 17, name: "Pessoa Exemplo"},
                company: {id: 23, name: "Soloz Industrial"},
                action: {
                    async doAction(action) {
                        actions.push(action);
                    },
                },
                store: {
                    notify(message, options) {
                        notifications.push({message, options});
                    },
                },
            };

            assert.notOk(
                await ContactPanel.prototype.openPartnerRecord.call(panel, 999),
                "a stale or forged partner ID is ignored"
            );
            assert.ok(await ContactPanel.prototype.openPartnerRecord.call(panel, 17));
            assert.deepEqual(actions, [
                {
                    type: "ir.actions.act_window",
                    name: "Pessoa Exemplo",
                    res_model: "res.partner",
                    res_id: 17,
                    views: [[false, "form"]],
                    view_mode: "form",
                    target: "new",
                },
            ]);

            panel.action.doAction = async () => {
                throw new Error("Access denied");
            };
            assert.notOk(
                await ContactPanel.prototype.openPartnerRecord.call(panel, 23)
            );
            assert.deepEqual(notifications, [
                {
                    message: "Não foi possível abrir este cadastro.",
                    options: {type: "danger", title: "Cadastro indisponível"},
                },
            ]);
        }
    );

    QUnit.test(
        "allows direct-company unlink only with the central-company capability",
        (assert) => {
            const canUnlinkIdentity = Object.getOwnPropertyDescriptor(
                ContactPanel.prototype,
                "canUnlinkIdentity"
            ).get;

            assert.ok(
                canUnlinkIdentity.call({
                    partner: {id: 17},
                    isLinkedCompany: false,
                    canLinkCentralCompany: false,
                }),
                "a linked person keeps the established unlink action"
            );
            assert.notOk(
                canUnlinkIdentity.call({
                    partner: {id: 23},
                    isLinkedCompany: true,
                    canLinkCentralCompany: false,
                }),
                "an agent cannot request a direct-company unlink"
            );
            assert.ok(
                canUnlinkIdentity.call({
                    partner: {id: 23},
                    isLinkedCompany: true,
                    canLinkCentralCompany: true,
                }),
                "a supervisor can unlink the central company"
            );
        }
    );

    QUnit.test(
        "unlinks only the expected current partner and ignores a stale response",
        async (assert) => {
            let resolveUnlink = null;
            const calls = [];
            const store = new ContactCenterStore({
                orm: {
                    async call(_model, method, args) {
                        calls.push({method, args});
                        return new Promise((resolve) => {
                            resolveUnlink = resolve;
                        });
                    },
                },
                busService: {},
                notification: false,
            });
            const firstIdentity = {
                id: 5,
                name: "Pessoa Exemplo",
                persona_kind: "contact",
                partner: {id: 17, name: "Pessoa Exemplo", is_company: false},
            };
            const replacementIdentity = {
                id: 5,
                name: "Cadastro mais novo",
                persona_kind: "contact",
                partner: {id: 18, name: "Cadastro mais novo", is_company: false},
            };
            store.state.selectedChannelId = 31;
            store.state.conversations = [
                openConversation({channel_id: 31, identity: firstIdentity}),
            ];

            const pending = store.unlinkPartner();
            assert.deepEqual(calls, [{method: "unlink_partner", args: [31, 17]}]);
            store.replaceConversation(
                openConversation({channel_id: 31, identity: replacementIdentity})
            );
            resolveUnlink({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                identity: {
                    id: 5,
                    name: "Convidado original",
                    persona_kind: "guest",
                    partner: false,
                },
            });
            assert.ok(await pending, "the server completed the guarded unlink");
            assert.strictEqual(
                store.state.conversations[0].identity.partner.id,
                18,
                "a late response cannot replace the newer linked partner"
            );
        }
    );

    QUnit.test("bounds deferred image work and prioritizes pending loads", (assert) => {
        const queue = new BoundedImageQueue(2);
        const started = [];
        const finishers = {};
        const loader = (name) => (finish) => {
            started.push(name);
            finishers[name] = finish;
            return () => started.push(`cancel:${name}`);
        };

        const cancelFirst = queue.schedule(loader("first"));
        queue.schedule(loader("second"));
        const cancelLow = queue.schedule(loader("low"), {priority: 1});
        queue.schedule(loader("high"), {priority: 10});

        assert.deepEqual(started, ["first", "second"]);
        assert.strictEqual(queue.activeCount, 2);
        assert.strictEqual(queue.pendingCount, 2);

        finishers.first();
        assert.deepEqual(started, ["first", "second", "high"]);
        assert.strictEqual(queue.activeCount, 2);
        assert.strictEqual(queue.pendingCount, 1);

        assert.ok(cancelLow(), "a pending load can be cancelled");
        assert.notOk(cancelLow(), "pending cancellation is idempotent");
        assert.strictEqual(queue.pendingCount, 0);

        assert.notOk(cancelFirst(), "a completed load cannot release twice");
        const cancellationQueue = new BoundedImageQueue(1);
        const activeStarted = [];
        const cancelActive = cancellationQueue.schedule(() => {
            activeStarted.push("active");
            return () => activeStarted.push("cancelled");
        });
        assert.ok(cancelActive(), "an active load can be cancelled");
        assert.notOk(cancelActive(), "active cancellation is idempotent");
        assert.deepEqual(activeStarted, ["active", "cancelled"]);
        assert.strictEqual(cancellationQueue.activeCount, 0);
    });

    QUnit.test(
        "alerts only once for an out-of-focus inbound message",
        async (assert) => {
            const listeners = {};
            const notifications = [];
            const documentScope = {
                hidden: true,
                title: "Odoo",
                hasFocus: () => false,
                addEventListener(name, callback) {
                    listeners[name] = callback;
                },
                removeEventListener(name, callback) {
                    if (listeners[name] === callback) {
                        delete listeners[name];
                    }
                },
            };
            class FakeNotification {
                constructor(title, options) {
                    notifications.push({title, options});
                }

                static requestPermission() {
                    FakeNotification.permission = "granted";
                    return Promise.resolve("granted");
                }
            }
            FakeNotification.permission = "default";
            const attention = new BrowserAttention({
                scope: {Notification: FakeNotification},
                documentScope,
            });
            const inbound = {
                event_type: "message_created",
                direction: "inbound",
                channel_id: 10,
                message_id: 20,
            };

            assert.notOk(
                attention.receive({...inbound, direction: "outbound"}),
                "outbound activity never alerts the agent"
            );
            assert.ok(attention.receive(inbound));
            assert.strictEqual(documentScope.title, "(1) Odoo");
            assert.notOk(attention.receive(inbound), "bus replay is deduplicated");
            assert.strictEqual(
                notifications.length,
                0,
                "permission is never requested implicitly"
            );

            await attention.enable();
            assert.strictEqual(attention.snapshot().permission, "granted");
            assert.ok(attention.receive({...inbound, message_id: 21}));
            assert.strictEqual(notifications.length, 1);
            assert.notOk(
                Object.prototype.hasOwnProperty.call(
                    notifications[0].options,
                    "body_text"
                ),
                "the lock-screen notification contains no message payload"
            );

            documentScope.hidden = false;
            documentScope.hasFocus = () => true;
            listeners.visibilitychange();
            assert.strictEqual(documentScope.title, "Odoo");
            assert.strictEqual(attention.snapshot().unseen, 0);
            attention.destroy();
            assert.notOk(
                listeners.visibilitychange,
                "the lifecycle listener is removed"
            );
        }
    );

    QUnit.test("extracts file transfers without intercepting pasted text", (assert) => {
        const image = {name: "captura.png"};
        assert.deepEqual(transferredFiles({files: [image]}), [image]);
        assert.deepEqual(
            transferredFiles({
                items: [
                    {kind: "string", getAsFile: () => null},
                    {kind: "file", getAsFile: () => image},
                ],
            }),
            [image]
        );
        assert.deepEqual(transferredFiles({items: [{kind: "string"}]}), []);
    });

    QUnit.test(
        "reports only the current deferred image failure after releasing its queue slot",
        (assert) => {
            const source = "/contact_center/media/17/content";
            let failures = 0;
            let failedGeneration = false;
            let finishes = 0;
            let removedSources = 0;
            const settlementOrder = [];
            const image = Object.create(DeferredImage.prototype);
            image.props = {
                loadGeneration: 3,
                onLoadError: (generation) => {
                    failures += 1;
                    failedGeneration = generation;
                    settlementOrder.push("owner");
                },
            };
            image.state = {activeSrc: false, status: "waiting"};
            image.destroyed = false;
            image.requestedSource = source;
            image.imageRef = {
                el: {
                    removeAttribute(name) {
                        assert.strictEqual(name, "src");
                        removedSources += 1;
                    },
                },
            };

            image._start(source, () => {
                finishes += 1;
                settlementOrder.push("queue");
            });
            assert.strictEqual(image.state.status, "loading");
            assert.strictEqual(image.state.activeSrc, source);
            image.onError();
            assert.strictEqual(image.state.status, "error");
            assert.strictEqual(image.state.activeSrc, false);
            assert.strictEqual(removedSources, 1);
            assert.strictEqual(finishes, 1, "the queue slot is released exactly once");
            assert.strictEqual(failures, 1, "the owner receives the current failure");
            assert.deepEqual(
                settlementOrder,
                ["queue", "owner"],
                "the queue slot is released before the owner replaces the image"
            );
            assert.strictEqual(
                failedGeneration,
                3,
                "the owner receives its generation"
            );
            image.onError();
            assert.strictEqual(failures, 1, "a settled image cannot report twice");

            const stale = Object.create(DeferredImage.prototype);
            stale.props = {onLoadError: () => (failures += 1)};
            stale.state = {activeSrc: false, status: "waiting"};
            stale.destroyed = false;
            stale.requestedSource = source;
            stale.imageRef = {
                el: {
                    removeAttribute() {
                        assert.ok(false, "a superseded source is never mutated");
                    },
                },
            };
            stale._start(source, () => (finishes += 1));
            stale.requestedSource = "/contact_center/media/18/content";
            stale.onError();
            assert.strictEqual(
                failures,
                1,
                "a callback from a superseded source never changes its owner"
            );
            assert.strictEqual(
                finishes,
                2,
                "the superseded load still releases the slot"
            );
        }
    );

    QUnit.test(
        "retries a failed media image with a fresh render generation",
        (assert) => {
            const content = Object.create(MessageContent.prototype);
            content.imageLoad = {attempts: {}, failures: {}};
            const media = {
                id: 17,
                kind: "image",
                content_url: "/contact_center/media/17/content",
            };

            assert.strictEqual(content.imageRenderKey(media), "id:17:0");
            assert.notOk(content.imageFailed(media));
            assert.ok(content.onImageLoadError(media, 0));
            assert.ok(
                content.imageFailed(media),
                "the failed image gets an explicit state"
            );

            const event = {
                prevented: false,
                stopped: false,
                preventDefault() {
                    this.prevented = true;
                },
                stopPropagation() {
                    this.stopped = true;
                },
            };
            assert.ok(content.retryImage(media, event));
            assert.ok(event.prevented);
            assert.ok(event.stopped, "retry never bubbles into the media viewer");
            assert.strictEqual(content.imageRenderKey(media), "id:17:1");
            assert.notOk(content.imageFailed(media));
            assert.notOk(
                content.onImageLoadError(media, 0),
                "a late callback from the previous render is ignored"
            );
            assert.ok(content.onImageLoadError(media, 1));
            assert.ok(content.imageFailed(media), "a failed retry remains recoverable");
        }
    );

    QUnit.test("validates versioned envelopes defensively", (assert) => {
        const payload = {schema_version: SUPPORTED_SCHEMA_VERSION, items: []};

        assert.strictEqual(validateEnvelope(payload), payload);
        assert.throws(
            () => validateEnvelope(null),
            TypeError,
            "null is not an envelope"
        );
        assert.throws(
            () => validateEnvelope([]),
            TypeError,
            "arrays are not envelopes"
        );
        assert.throws(
            () => validateEnvelope({schema_version: "1"}),
            TypeError,
            "the schema version is not coerced"
        );
        assert.throws(
            () => validateEnvelope({schema_version: 2}),
            RangeError,
            "future contracts fail closed"
        );
    });

    QUnit.test("normalizes search text and builds unicode initials", (assert) => {
        assert.strictEqual(
            normalizeSearchText("  ÁRVORE\ncom   AÇÃO  "),
            "arvore com acao"
        );
        assert.strictEqual(normalizeSearchText({toString: () => "unsafe"}), "");
        assert.strictEqual(initials("Maria da Silva"), "MS");
        assert.strictEqual(initials("Élodie"), "ÉL");
        assert.strictEqual(initials("李 小龍"), "李小");
        assert.strictEqual(initials(""), "?");
    });

    QUnit.test(
        "filters loaded conversations by responsibility without coercion",
        (assert) => {
            const mine = {channel_id: 1, responsible: {id: 7, name: "Lucas"}};
            const assigned = {channel_id: 2, responsible: {id: 8, name: "Ana"}};
            const unassigned = {channel_id: 3, responsible: false};
            const malformed = [
                {channel_id: 4},
                {channel_id: 5, responsible: null},
                {channel_id: 6, responsible: {id: "7", name: "Texto"}},
                {channel_id: 7, responsible: {id: 0, name: "Zero"}},
            ];
            const conversations = [mine, assigned, unassigned, ...malformed];
            const invalidItems = [
                null,
                1,
                [],
                {},
                {channel_id: "8", responsible: false},
                {channel_id: 0, responsible: false},
                {...mine, name: "Duplicate channel"},
            ];

            assert.strictEqual(conversationResponsibility(mine, 7), "mine");
            assert.strictEqual(conversationResponsibility(assigned, 7), "assigned");
            assert.strictEqual(conversationResponsibility(unassigned, 7), "unassigned");
            for (const conversation of malformed) {
                assert.strictEqual(
                    conversationResponsibility(conversation, 7),
                    "unknown",
                    "malformed responsibility fails closed"
                );
            }
            assert.strictEqual(
                conversationResponsibility(mine, "7"),
                "assigned",
                "the current user ID is never coerced"
            );
            assert.strictEqual(conversationResponsibility(null, 7), "unknown");
            assert.deepEqual(
                filterConversationsByResponsibility(conversations, "mine", 7),
                [mine]
            );
            assert.deepEqual(
                filterConversationsByResponsibility(conversations, "unassigned", 7),
                [unassigned],
                "only an explicit false is unassigned"
            );
            const all = filterConversationsByResponsibility(conversations, "all", 7);
            assert.deepEqual(all, conversations);
            assert.notStrictEqual(all, conversations, "the input array is not reused");
            assert.deepEqual(
                filterConversationsByResponsibility(
                    [...conversations, ...invalidItems],
                    "all",
                    7
                ),
                conversations,
                "invalid and duplicate rows cannot reach the Owl loop"
            );
            assert.ok(isRenderableConversation(mine));
            for (const item of invalidItems.slice(0, -1)) {
                assert.notOk(isRenderableConversation(item));
            }
            assert.deepEqual(
                filterConversationsByResponsibility(conversations, "unexpected", 7),
                [],
                "unknown scopes fail closed"
            );
        }
    );

    QUnit.test(
        "groups conversations by canonical inbox in alphabetical order",
        (assert) => {
            const first = Object.freeze({
                channel_id: 1,
                account: Object.freeze({id: 10, name: "Lucas", platform: "whatsapp"}),
                unread_count: 2,
            });
            const second = Object.freeze({
                channel_id: 2,
                account: Object.freeze({
                    id: 20,
                    name: "Comercial",
                    platform: "telegram",
                }),
                unread_count: 1,
            });
            const third = Object.freeze({
                channel_id: 3,
                account: Object.freeze({id: 10, name: "Nome alterado"}),
                unread_count: 3,
            });
            const sameNameDifferentId = Object.freeze({
                channel_id: 4,
                account: Object.freeze({id: 30, name: "Lucas", platform: "whatsapp"}),
            });
            const missingAccount = Object.freeze({channel_id: 5, account: false});
            const malformedAccount = Object.freeze({
                channel_id: 6,
                account: Object.freeze({id: "10", name: "Não confiar"}),
            });
            const nameless = Object.freeze({
                channel_id: 7,
                account: Object.freeze({id: 40, name: {toString: () => "unsafe"}}),
            });
            const conversations = Object.freeze([
                first,
                second,
                third,
                sameNameDifferentId,
                missingAccount,
                malformedAccount,
                nameless,
            ]);
            const groups = groupConversationsByInbox(conversations);

            assert.deepEqual(
                groups.map((group) => group.key),
                ["inbox:unknown", "inbox:40", "inbox:20", "inbox:10", "inbox:30"],
                "inbox activity does not change the alphabetical group order"
            );
            const groupsByKey = Object.fromEntries(
                groups.map((group) => [group.key, group])
            );
            assert.deepEqual(groupsByKey["inbox:10"].conversations, [first, third]);
            assert.strictEqual(groupsByKey["inbox:10"].name, "Lucas");
            assert.strictEqual(groupsByKey["inbox:10"].platform, "whatsapp");
            assert.strictEqual(groupsByKey["inbox:10"].unread_count, 5);
            assert.deepEqual(groupsByKey["inbox:unknown"].conversations, [
                missingAccount,
                malformedAccount,
            ]);
            assert.strictEqual(
                groupsByKey["inbox:unknown"].name,
                "Caixa não identificada"
            );
            assert.strictEqual(groupsByKey["inbox:40"].name, "Caixa sem nome");
            assert.strictEqual(conversations[0], first, "the source remains untouched");
            assert.deepEqual(conversationInboxMetadata(null), {
                key: "inbox:unknown",
                id: false,
                name: "Caixa não identificada",
                platform: "",
            });
            assert.deepEqual(groupConversationsByInbox(null), []);
            assert.deepEqual(
                groupConversationsByInbox([
                    null,
                    {channel_id: "1"},
                    first,
                    {...first},
                ])[0].conversations,
                [first],
                "grouping also filters malformed and duplicate rows"
            );
        }
    );

    QUnit.test(
        "keeps authorized inboxes visible before conversations load",
        (assert) => {
            const conversation = Object.freeze({
                channel_id: 1,
                account: Object.freeze({
                    id: 10,
                    name: "WhatsApp",
                    platform: "whatsapp",
                }),
                unread_count: 2,
            });
            const accounts = Object.freeze([
                Object.freeze({id: 10, name: "WhatsApp", platform: "whatsapp"}),
                Object.freeze({id: 20, name: "Instagram", platform: "instagram"}),
                Object.freeze({id: 30, name: "Messenger", platform: "messenger"}),
                Object.freeze({
                    id: "40",
                    name: "Conta inválida",
                    platform: "instagram",
                }),
            ]);

            const groups = groupConversationsByInbox([conversation], accounts);

            assert.deepEqual(
                groups.map((group) => group.key),
                ["inbox:20", "inbox:30", "inbox:10"],
                "loaded and empty inboxes share one alphabetical order"
            );
            assert.deepEqual(groups[2].conversations, [conversation]);
            assert.strictEqual(groups[2].unread_count, 2);
            assert.deepEqual(groups[0], {
                key: "inbox:20",
                id: 20,
                name: "Instagram",
                platform: "instagram",
                conversations: [],
                unread_count: 0,
            });
            assert.deepEqual(
                groupConversationsByInbox([], accounts).map((group) => group.key),
                ["inbox:20", "inbox:30", "inbox:10"],
                "the grouped view can render an entirely empty authorized fleet"
            );
        }
    );

    QUnit.test(
        "opens scoped media and reply for enabled groups and keeps participants distinct",
        (assert) => {
            const group = {
                conversation_type: "group",
                can_send: true,
                capabilities: {
                    media: {image: {enabled: true}},
                    reply: true,
                    react: true,
                },
            };

            assert.ok(isGroupConversation(group));
            assert.deepEqual(conversationUiPolicy(group), {
                is_group: true,
                show_composer: true,
                allow_send: true,
                allow_reply: true,
                allow_attachments: true,
                allow_react: true,
                allow_edit: false,
                allow_delete: false,
            });
            assert.deepEqual(
                conversationUiPolicy({...group, can_send: false}),
                {
                    is_group: true,
                    show_composer: false,
                    allow_send: false,
                    allow_reply: false,
                    allow_attachments: false,
                    allow_react: false,
                    allow_edit: false,
                    allow_delete: false,
                },
                "groups without an effective opt-in remain read-only"
            );
            assert.deepEqual(
                conversationUiPolicy({
                    conversation_type: "direct",
                    can_send: true,
                    capabilities: {media: {image: {enabled: true}}},
                }),
                {
                    is_group: false,
                    show_composer: true,
                    allow_send: true,
                    allow_reply: true,
                    allow_attachments: true,
                    allow_react: true,
                    allow_edit: true,
                    allow_delete: true,
                },
                "direct conversations keep their current composer contract"
            );
            assert.deepEqual(
                conversationUiPolicy({
                    conversation_type: "unexpected",
                    can_send: true,
                    capabilities: {media: {image: {enabled: true}}, react: true},
                }),
                {
                    is_group: false,
                    show_composer: false,
                    allow_send: false,
                    allow_reply: false,
                    allow_attachments: false,
                    allow_react: false,
                    allow_edit: false,
                    allow_delete: false,
                },
                "unknown conversation types fail closed"
            );
            assert.strictEqual(
                messageAuthorLabel({author: {name: "Ana"}, origin: "external"}),
                "Ana"
            );
            assert.strictEqual(
                messageAuthorLabel({author: {name: "Bruno"}, origin: "external"}),
                "Bruno",
                "each group item renders its participant instead of the group name"
            );
            const adversarialMessage = {
                actions: {reply: true, react: true, edit: true, delete: true},
            };
            assert.ok(messageActionEnabled(group, adversarialMessage, "reply"));
            assert.ok(messageActionEnabled(group, adversarialMessage, "react"));
            assert.notOk(
                messageActionEnabled(group, adversarialMessage, "edit"),
                "a stale per-message action cannot bypass a missing group capability"
            );
            assert.notOk(messageActionEnabled(group, adversarialMessage, "delete"));
            assert.notOk(
                messageActionEnabled(
                    {...group, can_send: false},
                    adversarialMessage,
                    "react"
                ),
                "a stale group action cannot bypass effective send policy"
            );
        }
    );

    QUnit.test(
        "gates each group mutation independently and closes revoked interactions",
        (assert) => {
            const group = {
                conversation_type: "group",
                can_send: true,
                capabilities: {
                    reply: true,
                    react: true,
                    edit_message: true,
                    delete_message: true,
                },
            };
            const message = {
                message_id: 71,
                actions: {reply: true, react: true, edit: true, delete: true},
            };
            for (const action of ["reply", "react", "edit", "delete"]) {
                assert.ok(
                    messageActionEnabled(group, message, action),
                    `${action} requires both effective and per-message authorization`
                );
            }
            assert.notOk(
                messageActionEnabled(
                    {
                        ...group,
                        capabilities: {...group.capabilities, edit_message: false},
                    },
                    message,
                    "edit"
                ),
                "one disabled capability does not inherit another action's grant"
            );
            assert.notOk(
                messageActionEnabled(
                    group,
                    {...message, actions: {...message.actions, react: false}},
                    "react"
                ),
                "the message projection remains authoritative"
            );

            const timeline = {
                ui: {
                    openMenuId: 71,
                    reactionPickerId: 71,
                    editingId: 71,
                    editBody: "Alteração pendente",
                    deletingId: 71,
                },
                state: {messages: [message]},
                store: {selectedConversation: group},
                messageById: ConversationTimeline.prototype.messageById,
                messageActionAllowed:
                    ConversationTimeline.prototype.messageActionAllowed,
                hasActions: ConversationTimeline.prototype.hasActions,
                cancelEdit: ConversationTimeline.prototype.cancelEdit,
            };
            ConversationTimeline.prototype.reconcileInteractionPolicy.call(timeline);
            assert.strictEqual(timeline.ui.reactionPickerId, 71);
            assert.strictEqual(timeline.ui.editingId, 71);
            assert.strictEqual(timeline.ui.deletingId, 71);

            timeline.store.selectedConversation = {
                ...group,
                can_send: false,
            };
            ConversationTimeline.prototype.reconcileInteractionPolicy.call(timeline);
            assert.notOk(timeline.ui.openMenuId, "the stale action menu closes");
            assert.notOk(timeline.ui.reactionPickerId, "the stale picker closes");
            assert.notOk(timeline.ui.editingId, "the stale editor closes");
            assert.strictEqual(timeline.ui.editBody, "", "the edit draft is discarded");
            assert.notOk(timeline.ui.deletingId, "the stale confirmation closes");
        }
    );

    QUnit.test(
        "keeps source-only menus open only while capability and event remain valid",
        (assert) => {
            const message = {
                message_id: 81,
                content_type: "call.accept",
                actions: {reply: false, react: false, edit: false, delete: false},
                source_inbox_event_id: 345,
            };
            const timeline = {
                ui: {
                    openMenuId: 81,
                    reactionPickerId: false,
                    editingId: false,
                    editBody: "",
                    deletingId: false,
                },
                state: {messages: [message]},
                store: {
                    capabilities: {view_source_webhook: true},
                    selectedConversation: {
                        conversation_type: "direct",
                        can_send: true,
                    },
                },
                messageById: ConversationTimeline.prototype.messageById,
                messageActionAllowed:
                    ConversationTimeline.prototype.messageActionAllowed,
                hasActions: ConversationTimeline.prototype.hasActions,
                cancelEdit: ConversationTimeline.prototype.cancelEdit,
            };
            assert.ok(
                ConversationTimeline.prototype.hasActions.call(timeline, message),
                "a diagnostic-only message still exposes its menu"
            );
            const revision = Object.getOwnPropertyDescriptor(
                ConversationTimeline.prototype,
                "interactionAuthorizationRevision"
            ).get;
            const grantedRevision = revision.call(timeline);

            timeline.store.capabilities.view_source_webhook = false;
            assert.notStrictEqual(
                revision.call(timeline),
                grantedRevision,
                "the authorization revision includes the bootstrap capability"
            );
            ConversationTimeline.prototype.reconcileInteractionPolicy.call(timeline);
            assert.notOk(
                timeline.ui.openMenuId,
                "a revoked capability closes the menu"
            );

            timeline.store.capabilities.view_source_webhook = true;
            timeline.ui.openMenuId = 81;
            const validEventRevision = revision.call(timeline);
            message.source_inbox_event_id = false;
            assert.notStrictEqual(
                revision.call(timeline),
                validEventRevision,
                "the authorization revision includes the source event ID"
            );
            ConversationTimeline.prototype.reconcileInteractionPolicy.call(timeline);
            assert.notOk(timeline.ui.openMenuId, "a revoked source ID closes the menu");
        }
    );

    QUnit.test(
        "opens the exact current source webhook through the action service",
        async (assert) => {
            const actions = [];
            const message = {
                message_id: 82,
                content_type: "identity.security.changed",
                actions: {reply: false, react: false, edit: false, delete: false},
                source_inbox_event_id: 346,
            };
            const timeline = {
                ui: {
                    openMenuId: 82,
                    reactionPickerId: false,
                    editingId: false,
                    editBody: "",
                    deletingId: false,
                },
                state: {messages: [message]},
                store: {
                    capabilities: {view_source_webhook: true},
                    selectedConversation: {
                        conversation_type: "direct",
                        can_send: true,
                    },
                },
                action: {
                    doAction: async (action) => actions.push(action),
                },
                messageById: ConversationTimeline.prototype.messageById,
                messageActionAllowed:
                    ConversationTimeline.prototype.messageActionAllowed,
                canViewSourceWebhook:
                    ConversationTimeline.prototype.canViewSourceWebhook,
                hasActions: ConversationTimeline.prototype.hasActions,
                reconcileInteractionPolicy:
                    ConversationTimeline.prototype.reconcileInteractionPolicy,
                cancelEdit: ConversationTimeline.prototype.cancelEdit,
                closeActions: ConversationTimeline.prototype.closeActions,
            };

            assert.ok(
                await ConversationTimeline.prototype.viewSourceWebhook.call(timeline, {
                    message_id: 82,
                    source_inbox_event_id: 999,
                })
            );
            assert.deepEqual(actions, [
                {
                    type: "ir.actions.act_window",
                    name: "Webhook de origem",
                    res_model: "contact.center.inbox.event",
                    res_id: 346,
                    views: [[false, "form"]],
                    view_mode: "form",
                    target: "new",
                },
            ]);
            assert.notOk(timeline.ui.openMenuId, "the menu closes before navigation");

            timeline.ui.openMenuId = 82;
            timeline.store.capabilities.view_source_webhook = false;
            assert.notOk(
                await ConversationTimeline.prototype.viewSourceWebhook.call(
                    timeline,
                    message
                ),
                "the handler revalidates a revoked capability"
            );
            assert.strictEqual(actions.length, 1, "revocation never reaches doAction");
            assert.notOk(
                timeline.ui.openMenuId,
                "revocation reconciles the stale menu"
            );
        }
    );

    QUnit.test("groups only consecutive messages from the same sender", (assert) => {
        const previous = {
            author: {type: "guest", id: 21, name: "Ana"},
            body_text: "Primeira",
            date: "2026-08-24 10:00:00",
            direction: "inbound",
            origin: "provider",
        };
        const current = {
            ...previous,
            body_text: "Segunda",
            date: "2026-08-24 10:04:59",
        };

        assert.ok(
            isMessageContinuation(current, previous),
            "the run remains open below five minutes"
        );
        assert.notOk(
            isMessageContinuation({...current, date: "2026-08-24 10:05:00"}, previous),
            "the exact five-minute boundary starts a new run"
        );
        assert.notOk(
            isMessageContinuation(
                {...current, author: {type: "guest", id: 22, name: "Ana"}},
                previous
            ),
            "equal display names never merge different identities"
        );
        assert.notOk(
            isMessageContinuation({...current, origin: "external_device"}, previous),
            "a device echo is visually distinct from the provider sender"
        );
        assert.notOk(
            isMessageContinuation({...current, direction: "outbound"}, previous),
            "direction changes always break the run"
        );
        assert.notOk(
            isMessageContinuation({...current, date: "2026-08-25 00:01:00"}, previous),
            "day boundaries always break the run"
        );
        assert.notOk(
            isMessageContinuation({...current, is_deleted: true}, previous),
            "deleted messages stand alone"
        );
        assert.notOk(
            isMessageContinuation({...current, direction: "internal"}, previous),
            "internal notes stand alone"
        );
        assert.notOk(
            isMessageContinuation({...current, author: {name: "Ana"}}, previous),
            "malformed author DTOs fail closed"
        );
        assert.notOk(
            isMessageContinuation(
                {...current, author: {type: "system", id: 1, name: "Sistema"}},
                previous
            ),
            "non-person authors never form a run"
        );
        assert.notOk(
            isMessageContinuation(
                {...current, body_text: "", media: [null]},
                {...previous, body_text: "", media: [null]}
            ),
            "malformed media does not make an empty message renderable"
        );
        assert.ok(
            isMessageContinuation(
                {...current, body_text: "", media: [{kind: "image"}]},
                previous
            ),
            "media-only messages can remain in the sender run"
        );
    });

    QUnit.test(
        "renders provider-neutral control events outside message runs",
        (assert) => {
            const supportedTypes = [
                "call.offer",
                "call.accept",
                "call.terminate",
                "identity.security.changed",
            ];
            for (const contentType of supportedTypes) {
                assert.ok(
                    isControlTimelineMessage({content_type: contentType}),
                    `${contentType} has an explicit control presentation`
                );
            }
            assert.notOk(
                isControlTimelineMessage({content_type: "provider.call.offer"}),
                "provider-specific values fail closed"
            );
            assert.notOk(
                isControlTimelineMessage({content_type: "toString"}),
                "object prototype keys never select a control presentation"
            );
            assert.notOk(
                isControlTimelineMessage(
                    Object.create({content_type: "identity.security.changed"})
                ),
                "inherited values never select the control presentation"
            );

            const personMessage = {
                author: {type: "guest", id: 21, name: "Ana"},
                body_text: "Mensagem",
                content_type: "text",
                date: "2026-08-24 10:00:00",
                direction: "inbound",
                origin: "provider",
            };
            const controlMessage = {
                ...personMessage,
                body_text: "Chamada recebida",
                content_type: "call.offer",
            };
            assert.notOk(
                isMessageContinuation(controlMessage, personMessage),
                "a control event never continues a person's run"
            );
            assert.notOk(
                isMessageContinuation(personMessage, controlMessage),
                "a message after a control event starts a new run"
            );

            const timeline = {
                state: {messages: [controlMessage]},
                store: {capabilities: {view_source_webhook: false}},
                isGroupConversation: false,
                isRunContinuation: () => false,
                endsRun: () => true,
                time: () => "10:00",
                messageActionAllowed: () => true,
            };
            const className = ConversationTimeline.prototype.messageClass.call(
                timeline,
                controlMessage,
                0
            );
            assert.ok(className.includes("cc-message--control"));
            assert.notOk(
                ConversationTimeline.prototype.hasActions.call(
                    timeline,
                    controlMessage
                ),
                "control events without an authorized source have no actions"
            );
            const ariaLabel = ConversationTimeline.prototype.controlAriaLabel.call(
                timeline,
                {
                    ...controlMessage,
                    provider: "provider-internal",
                    external_message_id: "opaque-provider-id",
                }
            );
            assert.strictEqual(ariaLabel, "Evento de chamada: Chamada recebida. 10:00");
            assert.notOk(ariaLabel.includes("provider-internal"));
            assert.notOk(ariaLabel.includes("opaque-provider-id"));
        }
    );

    QUnit.test("control events expose only the authorized source action", (assert) => {
        const message = {
            message_id: 83,
            content_type: "call.accept",
            source_inbox_event_id: 347,
            actions: {reply: true, react: true, edit: true, delete: true, resend: true},
        };
        const timeline = {
            store: {
                capabilities: {view_source_webhook: true},
                selectedConversation: {conversation_type: "direct", can_send: true},
            },
            messageActionAllowed: ConversationTimeline.prototype.messageActionAllowed,
        };
        for (const contentType of [
            "call.offer",
            "call.accept",
            "call.terminate",
            "identity.security.changed",
        ]) {
            message.content_type = contentType;
            assert.ok(
                ConversationTimeline.prototype.hasActions.call(timeline, message),
                `${contentType} exposes the diagnostic-only menu`
            );
            for (const action of ["reply", "react", "edit", "delete", "resend"]) {
                assert.notOk(
                    timeline.messageActionAllowed(message, action),
                    `${contentType} never allows ${action}, even with stale DTO actions`
                );
            }
        }
        timeline.store.capabilities.view_source_webhook = false;
        assert.notOk(ConversationTimeline.prototype.hasActions.call(timeline, message));
        timeline.store.capabilities.view_source_webhook = true;
        message.source_inbox_event_id = false;
        assert.notOk(ConversationTimeline.prototype.hasActions.call(timeline, message));
    });

    QUnit.test(
        "rendered control and message menus retain their own source and keyboard behavior",
        async (assert) => {
            const actions = [];
            registry.category("services").add("action", {
                start: () => ({doAction: async (action) => actions.push(action)}),
            });
            registry.category("services").add("dialog", {
                start: () => ({add: () => () => undefined}),
            });
            makeFakeLocalizationService();
            const env = await makeTestEnv();
            const target = getFixture();
            const store = new ContactCenterStore({
                orm: {},
                busService: new EventTarget(),
            });
            store.state.bootstrap = {capabilities: {view_source_webhook: true}};
            store.state.selectedChannelId = 10;
            store.state.conversations = [
                openConversation({channel_id: 10, conversation_type: "direct"}),
            ];
            store.state.timelinePhase = "ready";
            store.state.messages = ["call.accept", "text"].map(
                (contentType, index) => ({
                    message_id: 84 + index,
                    source_inbox_event_id: 348 + index,
                    content_type: contentType,
                    body_text: contentType === "text" ? "Mensagem" : "Chamada atendida",
                    date: "2026-08-29 12:00:00",
                    direction: "inbound",
                    origin: "provider",
                    platform: "whatsapp",
                    provider: "wuzapi",
                    actions: {reply: false, react: false, edit: false, delete: false},
                    media: [],
                    reactions: [],
                })
            );
            try {
                await mount(ConversationTimeline, target, {
                    env,
                    props: {state: store.state, store},
                });
                assert.strictEqual(
                    target.querySelectorAll(".cc-message--control").length,
                    1
                );
                for (const id of [84, 85]) {
                    const row = `[data-message-id="${id}"]`;
                    const toggle = `${row} .cc-message-actions__toggle`;
                    await click(target, toggle);
                    const menu = target.querySelector(`${row} .cc-message-menu`);
                    assert.strictEqual(
                        menu.querySelectorAll('[role="menuitem"]').length,
                        1
                    );
                    assert.strictEqual(
                        menu.textContent.trim(),
                        "Ver webhook de origem"
                    );
                    menu.dispatchEvent(
                        new KeyboardEvent("keydown", {key: "ArrowDown", bubbles: true})
                    );
                    assert.strictEqual(
                        document.activeElement,
                        menu.querySelector("button")
                    );
                    menu.dispatchEvent(
                        new KeyboardEvent("keydown", {key: "Escape", bubbles: true})
                    );
                    await nextTick();
                    assert.notOk(target.querySelector(`${row} .cc-message-menu`));
                    assert.strictEqual(
                        document.activeElement,
                        target.querySelector(toggle)
                    );
                    await click(target, toggle);
                    await click(target, `${row} .cc-message-menu [role="menuitem"]`);
                    assert.strictEqual(actions[actions.length - 1].res_id, id + 264);
                }
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test("sanitizes viewer media and formats audio time", (assert) => {
        const safeImage = {
            id: 8,
            kind: "image",
            state: "ready",
            name: "painel.jpg",
            mimetype: "image/jpeg",
            content_url: "/contact_center/media/8/content",
            download_url: "/contact_center/media/8/content?download=1",
        };
        const items = viewableMediaItems([
            safeImage,
            {...safeImage, id: 9, kind: "audio"},
            {...safeImage, id: 10, state: "pending"},
            {...safeImage, id: 11, content_url: "https://provider.invalid/x"},
            null,
        ]);

        assert.strictEqual(items.length, 1);
        assert.deepEqual(items[0], safeImage);
        assert.ok(isLocalMediaUrl("/contact_center/media/8/content"));
        assert.ok(isLocalMediaUrl("/contact_center/media/8/content?download=1"));
        assert.notOk(isLocalMediaUrl("//provider.invalid/file"));
        assert.notOk(isLocalMediaUrl("/contact_center/media/nope/content"));
        assert.strictEqual(formatAudioTime(0), "0:00");
        assert.strictEqual(formatAudioTime(69.9), "1:09");
        assert.strictEqual(formatAudioTime(3661), "61:01");
        assert.strictEqual(formatAudioTime(-1), "0:00");
        assert.strictEqual(formatAudioTime("69"), "0:00");
    });

    QUnit.test("opens only authenticated local PDFs in the PDF.js viewer", (assert) => {
        const safePdf = {
            id: 42,
            kind: "document",
            state: "ready",
            name: "fatura agosto.pdf",
            mimetype: "application/pdf",
            content_url: "/contact_center/media/42/content",
            download_url: "/contact_center/media/42/content?download=1",
        };
        const [pdf] = viewableMediaItems([safePdf]);

        assert.ok(isViewablePdf(safePdf));
        assert.strictEqual(pdf.id, 42);
        assert.strictEqual(pdf.kind, "document");
        assert.strictEqual(pdf.content_url, safePdf.content_url);
        assert.strictEqual(pdf.download_url, safePdf.download_url);
        assert.strictEqual(
            pdf.pdf_viewer_url,
            "/web/static/lib/pdfjs/web/viewer.html?file=%2Fcontact_center%2Fmedia%2F42%2Fcontent#pagemode=none",
            "the authenticated media URL is encoded as PDF.js data, not executable markup"
        );
        assert.strictEqual(
            pdfJsViewerUrl(safePdf.content_url),
            pdf.pdf_viewer_url,
            "the shared builder produces the same local viewer URL"
        );

        const [normalizedMime] = viewableMediaItems([
            {
                ...safePdf,
                mimetype: "  Application/PDF ; charset=binary  ",
            },
        ]);
        assert.ok(normalizedMime, "MIME case, whitespace and parameters normalize");
        assert.strictEqual(normalizedMime.mimetype, "application/pdf");

        const [sanitizedDownload] = viewableMediaItems([
            {
                ...safePdf,
                download_url: "https://provider.invalid/fatura.pdf",
            },
        ]);
        assert.strictEqual(
            sanitizedDownload.download_url,
            safePdf.content_url,
            "an untrusted download URL falls back to the authenticated content route"
        );
        const [mismatchedLocalDownload] = viewableMediaItems([
            {
                ...safePdf,
                download_url: "/contact_center/media/41/content?download=1",
            },
        ]);
        assert.strictEqual(
            mismatchedLocalDownload.download_url,
            safePdf.content_url,
            "a local download URL for another media ID also fails closed"
        );
    });

    QUnit.test("rejects non-PDF and untrusted PDF viewer inputs", (assert) => {
        const javascriptUrl = ["java", "script:alert(1)"].join("");
        const safePdf = {
            id: 42,
            kind: "document",
            state: "ready",
            name: "fatura.pdf",
            mimetype: "application/pdf",
            content_url: "/contact_center/media/42/content",
            download_url: "/contact_center/media/42/content?download=1",
        };
        const rejectedMedia = [
            {...safePdf, mimetype: "text/plain"},
            {...safePdf, mimetype: "application/pdffoo"},
            {...safePdf, content_url: "https://provider.invalid/fatura.pdf"},
            {...safePdf, content_url: "//provider.invalid/fatura.pdf"},
            {...safePdf, content_url: "data:application/pdf;base64,AA=="},
            {...safePdf, content_url: javascriptUrl},
            {...safePdf, content_url: "/contact_center/media/42/content?download=1"},
            {...safePdf, id: 43},
            {...safePdf, state: "pending"},
        ];

        assert.deepEqual(viewableMediaItems(rejectedMedia), []);
        for (const media of rejectedMedia) {
            assert.notOk(
                isViewablePdf(media),
                `${media.mimetype} at ${media.content_url} is rejected`
            );
        }
        for (const contentUrl of [
            "https://provider.invalid/fatura.pdf",
            "//provider.invalid/fatura.pdf",
            "data:application/pdf;base64,AA==",
            javascriptUrl,
            "/contact_center/media/42/content?download=1",
        ]) {
            assert.strictEqual(
                pdfJsViewerUrl(contentUrl),
                "",
                `${contentUrl} cannot reach PDF.js`
            );
        }
    });

    QUnit.test("keeps existing image and video viewer items unchanged", (assert) => {
        const image = {
            id: 8,
            kind: "image",
            state: "ready",
            name: "painel.jpg",
            mimetype: "image/jpeg",
            content_url: "/contact_center/media/8/content",
            download_url: "/contact_center/media/8/content?download=1",
        };
        const video = {
            ...image,
            id: 9,
            kind: "video",
            name: "instalacao.mp4",
            mimetype: "video/mp4",
            content_url: "/contact_center/media/9/content",
            download_url: "/contact_center/media/9/content?download=1",
        };

        assert.deepEqual(viewableMediaItems([image, video]), [image, video]);
        assert.notOk(isViewablePdf(image));
        assert.notOk(isViewablePdf(video));
    });

    QUnit.test("allow-lists the complete group metadata contract", (assert) => {
        const defaults = {
            display_name: "Grupo legado",
            avatar_url: false,
            participant_count: false,
            admin_count: false,
            own_role: "unknown",
            metadata_state: "unavailable",
            last_synced_at: false,
        };
        assert.deepEqual(
            normalizeGroupMetadata({conversation_type: "group", group: defaults}),
            defaults,
            "the canonical unavailable projection is valid"
        );
        const source = canonicalGroup({
            display_name: '<img src=x onerror="globalThis.pwned=true">',
            avatar_url: "/contact_center/group/9/avatar",
            roster: [{jid: "hidden@s.whatsapp.net"}],
            token: "hidden",
            raw_payload: {provider: "hidden"},
        });
        const conversation = {
            conversation_type: "group",
            name: "Fallback",
            group: source,
        };
        const normalized = normalizeGroupMetadata(conversation);

        assert.deepEqual(normalized, {
            display_name: '<img src=x onerror="globalThis.pwned=true">',
            avatar_url: "/contact_center/group/9/avatar",
            participant_count: 12,
            admin_count: 2,
            own_role: "member",
            metadata_state: "ready",
            last_synced_at: "2026-08-24 11:30:00",
        });
        assert.notStrictEqual(
            normalized,
            source,
            "the provider object is never reused"
        );
        assert.notOk("roster" in normalized);
        assert.notOk("token" in normalized);
        assert.notOk("raw_payload" in normalized);
        assert.deepEqual(groupMetadataUi(conversation), normalized);
        assert.deepEqual(groupMetadataStateMeta("ready"), {
            state: "ready",
            label: "Sincronizado",
            description: "Os detalhes do grupo estão atualizados.",
            tone: "success",
        });
        assert.deepEqual(groupRoleMeta("superadmin"), {
            role: "superadmin",
            label: "Superadministrador",
        });
        assert.strictEqual(
            conversationDisplayName(conversation),
            normalized.display_name
        );
        assert.strictEqual(conversationGroupBadgeLabel(conversation), "Grupo · 12");
        assert.strictEqual(
            conversationGroupBadgeLabel({
                ...conversation,
                group: canonicalGroup({participant_count: 0, admin_count: false}),
            }),
            "Grupo · 0",
            "zero remains visible in the list"
        );
    });

    QUnit.test("renders only safe local conversation avatars", (assert) => {
        const direct = {
            conversation_type: "direct",
            identity: {
                avatar_url: "/contact_center/identity/7/avatar?v=abc123",
            },
        };
        assert.strictEqual(
            conversationAvatarUrl(direct),
            "/contact_center/identity/7/avatar?v=abc123",
            "a canonical identity route is accepted for a direct conversation"
        );

        for (const avatarUrl of [
            "https://provider.invalid/avatar",
            "//provider.invalid/avatar",
            "/\\provider.invalid/avatar",
            "/\t/provider.invalid/avatar",
            "data:image/png;base64,AA==",
            "blob:https://provider.invalid/id",
            false,
            null,
        ]) {
            assert.strictEqual(
                conversationAvatarUrl({
                    ...direct,
                    identity: {avatar_url: avatarUrl},
                }),
                false,
                `${String(avatarUrl)} cannot become an individual avatar source`
            );
        }

        const group = {
            conversation_type: "group",
            identity: {avatar_url: "/contact_center/identity/7/avatar"},
            group: canonicalGroup({
                avatar_url: "/contact_center/group/9/avatar?v=def456",
            }),
        };
        assert.strictEqual(
            conversationAvatarUrl(group),
            "/contact_center/group/9/avatar?v=def456",
            "group metadata remains authoritative"
        );
        assert.strictEqual(
            conversationAvatarUrl({
                ...group,
                group: canonicalGroup({avatar_url: false}),
            }),
            false,
            "a group never falls back to an unrelated direct identity avatar"
        );
        assert.strictEqual(
            conversationAvatarUrl({
                conversation_type: "direct",
                identity: Object.create({
                    avatar_url: "/contact_center/identity/inherited/avatar",
                }),
            }),
            false,
            "inherited identity properties fail closed"
        );
    });

    QUnit.test("keeps message-run avatar and spacer on the same lane", (assert) => {
        const fixture = document.getElementById("qunit-fixture");
        const app = document.createElement("div");
        app.className = "o_contact_center_ui";
        app.innerHTML = `
            <div class="cc-timeline">
                <div class="cc-timeline__inner">
                    <article class="cc-message cc-message--inbound cc-message--run-start">
                        <span class="cc-message__avatar cc-avatar">AZ</span>
                        <div class="cc-message__content">
                            <div class="cc-message__source">Autor</div>
                            <div class="cc-message__lane">
                                <div class="cc-message__stack">
                                    <div class="cc-message__bubble"><p>Primeira</p></div>
                                </div>
                            </div>
                        </div>
                    </article>
                    <article class="cc-message cc-message--inbound cc-message--run-continuation">
                        <span class="cc-message__avatar-spacer"></span>
                        <div class="cc-message__content">
                            <div class="cc-message__lane">
                                <div class="cc-message__stack">
                                    <div class="cc-message__bubble"><p>Segunda</p></div>
                                </div>
                            </div>
                        </div>
                    </article>
                </div>
            </div>`;
        fixture.appendChild(app);

        const avatar = app.querySelector(".cc-message__avatar");
        const spacer = app.querySelector(".cc-message__avatar-spacer");
        const bubbles = app.querySelectorAll(".cc-message__bubble");
        const avatarStyle = window.getComputedStyle(avatar);
        const spacerStyle = window.getComputedStyle(spacer);
        const laneSize = window
            .getComputedStyle(app.querySelector(".cc-message"))
            .getPropertyValue("--cc-message-avatar-size")
            .trim();

        assert.ok(laneSize, "the shared avatar lane custom property is active");
        assert.ok(parseFloat(avatarStyle.width) > 0, "the avatar has a real width");
        assert.strictEqual(
            spacerStyle.width,
            avatarStyle.width,
            "the continuation spacer has the avatar width"
        );
        assert.strictEqual(
            spacerStyle.flexBasis,
            avatarStyle.width,
            "the non-shrinking spacer reserves the complete avatar lane"
        );
        assert.ok(
            Math.abs(
                bubbles[0].getBoundingClientRect().left -
                    bubbles[1].getBoundingClientRect().left
            ) < 0.51,
            "consecutive inbound bubbles share the same left edge"
        );
    });

    QUnit.test("fails closed for incomplete or malformed group metadata", (assert) => {
        const fields = [
            "display_name",
            "avatar_url",
            "participant_count",
            "admin_count",
            "own_role",
            "metadata_state",
            "last_synced_at",
        ];
        for (const field of fields) {
            const group = canonicalGroup();
            delete group[field];
            assert.strictEqual(
                normalizeGroupMetadata({conversation_type: "group", group}),
                false,
                `${field} is required by v1`
            );
        }

        for (const value of [false, 0, 1, Number.MAX_SAFE_INTEGER]) {
            const group = canonicalGroup({
                participant_count: value,
                admin_count: value === false ? false : 0,
            });
            assert.strictEqual(
                normalizeGroupMetadata({conversation_type: "group", group})
                    .participant_count,
                value,
                `participant count ${String(value)} is preserved`
            );
        }
        for (const value of [
            -1,
            1.5,
            NaN,
            Infinity,
            Number.MAX_SAFE_INTEGER + 1,
            "0",
            null,
            true,
        ]) {
            assert.strictEqual(
                normalizeGroupMetadata({
                    conversation_type: "group",
                    group: canonicalGroup({participant_count: value}),
                }),
                false,
                `invalid count ${String(value)} is rejected`
            );
        }
        assert.strictEqual(
            normalizeGroupMetadata({
                conversation_type: "group",
                group: canonicalGroup({participant_count: 2, admin_count: 3}),
            }),
            false,
            "an impossible administrator count is rejected"
        );

        for (const role of ["unknown", "member", "admin", "superadmin"]) {
            assert.strictEqual(
                normalizeGroupMetadata({
                    conversation_type: "group",
                    group: canonicalGroup({own_role: role}),
                }).own_role,
                role
            );
        }
        for (const state of ["unavailable", "pending", "ready", "stale", "failed"]) {
            assert.strictEqual(
                normalizeGroupMetadata({
                    conversation_type: "group",
                    group: canonicalGroup({metadata_state: state}),
                }).metadata_state,
                state
            );
        }
        for (const value of ["", "ADMIN", " admin ", null, {}]) {
            assert.strictEqual(
                normalizeGroupMetadata({
                    conversation_type: "group",
                    group: canonicalGroup({own_role: value}),
                }),
                false
            );
        }
        for (const value of ["", "READY", " ready ", null, {}]) {
            assert.strictEqual(
                normalizeGroupMetadata({
                    conversation_type: "group",
                    group: canonicalGroup({metadata_state: value}),
                }),
                false
            );
        }
        for (const avatarUrl of [
            "https://provider.invalid/avatar",
            "//provider.invalid/avatar",
            "/\\provider.invalid/avatar",
            "/\t/provider.invalid/avatar",
            ["java", "script:alert(1)"].join(""),
            "data:image/png;base64,AA==",
            "blob:https://provider.invalid/id",
        ]) {
            assert.strictEqual(
                normalizeGroupMetadata({
                    conversation_type: "group",
                    group: canonicalGroup({avatar_url: avatarUrl}),
                }),
                false,
                `${avatarUrl} cannot become an avatar source`
            );
        }
        assert.strictEqual(
            normalizeGroupMetadata({
                conversation_type: "group",
                group: canonicalGroup({last_synced_at: "2026-99-99 99:99:99"}),
            }),
            false,
            "invalid Odoo datetimes are rejected"
        );
        const inherited = Object.assign(Object.create(canonicalGroup()), {
            display_name: "Only one own field",
        });
        assert.strictEqual(
            normalizeGroupMetadata({conversation_type: "group", group: inherited}),
            false,
            "inherited fields never satisfy the contract"
        );

        const fallback = groupMetadataUi({
            conversation_type: "group",
            name: "Nome legado",
            group: {display_name: "incomplete"},
        });
        assert.deepEqual(fallback, {
            display_name: "Nome legado",
            avatar_url: false,
            participant_count: false,
            admin_count: false,
            own_role: "unknown",
            metadata_state: "unavailable",
            last_synced_at: false,
        });
        assert.strictEqual(groupMetadataUi({conversation_type: "direct"}), false);
        assert.strictEqual(
            conversationDisplayName({
                conversation_type: "direct",
                name: "Direct unchanged",
                group: canonicalGroup({display_name: "Injected"}),
            }),
            "Direct unchanged",
            "group metadata never changes a direct conversation name"
        );
    });

    QUnit.test("normalizes group metadata at every conversation ingress", (assert) => {
        const store = new ContactCenterStore({
            orm: {},
            busService: {},
            notification: false,
        });
        const valid = {
            channel_id: 10,
            state: "open",
            conversation_type: "group",
            name: "Legacy",
            group: canonicalGroup({token: "must-not-survive"}),
        };
        const direct = {
            channel_id: 20,
            state: "open",
            conversation_type: "direct",
            name: "Direct unchanged",
            can_send: true,
            group: canonicalGroup(),
        };

        store.applyConversationPage(
            {items: [valid, direct], has_more: false},
            {reset: true, silent: false, previousConversation: false}
        );
        assert.strictEqual(store.state.conversations.length, 2);
        assert.notOk("token" in store.state.conversations[0].group);
        assert.strictEqual(valid.group.token, "must-not-survive", "input is immutable");
        assert.strictEqual(store.state.conversations[1].group, false);
        assert.strictEqual(store.state.conversations[1].name, "Direct unchanged");
        assert.ok(conversationUiPolicy(store.state.conversations[1]).show_composer);

        store.applyConversationPage(
            {
                items: [
                    {
                        ...valid,
                        group: {display_name: "malformed update"},
                    },
                ],
                has_more: false,
            },
            {reset: false, silent: false, previousConversation: false}
        );
        assert.strictEqual(
            store.state.conversations.find((item) => item.channel_id === 10).group,
            false,
            "pagination replaces valid metadata with a fail-closed update"
        );

        store.replaceConversation(valid);
        assert.ok(store.state.conversations[0].group);
        store.replaceConversation({...valid, group: null});
        assert.strictEqual(store.state.conversations[0].group, false);
        assert.notOk(conversationUiPolicy(store.state.conversations[0]).show_composer);
        const beforeInvalidReplacement = [...store.state.conversations];
        assert.notOk(store.replaceConversation(null));
        assert.notOk(store.replaceConversation({channel_id: "10"}));
        assert.deepEqual(
            store.state.conversations,
            beforeInvalidReplacement,
            "malformed refreshes never contaminate the store"
        );
        assert.strictEqual(
            normalizeConversationGroup(direct).group,
            false,
            "direct conversations ignore injected group metadata"
        );
    });

    QUnit.test("allow-lists attribution without technical evidence", (assert) => {
        const raw = {
            enabled: true,
            items: [
                {
                    public_ref: "0a0b0c0d-1111-4222-8333-444455556666",
                    touchpoint_type: "paid_ad_click",
                    evidence_level: "provider_asserted",
                    network: "meta",
                    source_platform: "instagram",
                    source_type: "ad",
                    entry_point_source: "ctwa_ad",
                    entry_point_app: "instagram",
                    utm_source: "instagram",
                    utm_medium: "paid_social",
                    utm_campaign: "Campanha segura",
                    creative_media_type: "video",
                    show_ad_attribution: true,
                    occurred_at: "2026-08-25 14:30:00",
                    source_url: "https://provider.invalid/private",
                    ctwaClid: "must-not-survive",
                    sourceID: "must-not-survive",
                    provider_extensions: {secret: "must-not-survive"},
                },
            ],
            has_more: false,
            next_cursor: false,
        };

        const normalized = normalizeAttributionProjection(raw);

        assert.deepEqual(normalized, {
            enabled: true,
            items: [
                {
                    public_ref: "0a0b0c0d-1111-4222-8333-444455556666",
                    touchpoint_type: "paid_ad_click",
                    evidence_level: "provider_asserted",
                    network: "meta",
                    source_platform: "instagram",
                    source_type: "ad",
                    entry_point_source: "ctwa_ad",
                    entry_point_app: "instagram",
                    utm_source: "instagram",
                    utm_medium: "paid_social",
                    utm_campaign: "Campanha segura",
                    creative_media_type: "video",
                    show_ad_attribution: true,
                    occurred_at: "2026-08-25 14:30:00",
                },
            ],
            has_more: false,
            next_cursor: false,
        });
        const serialized = JSON.stringify(normalized);
        for (const forbidden of [
            "provider.invalid",
            "ctwaClid",
            "sourceID",
            "provider_extensions",
            "must-not-survive",
        ]) {
            assert.notOk(serialized.includes(forbidden), `${forbidden} is removed`);
        }
        assert.deepEqual(
            normalizeAttributionProjection({
                enabled: false,
                items: [{source_url: "must-not-survive"}],
            }),
            {enabled: false, items: [], has_more: false, next_cursor: false}
        );
        assert.strictEqual(
            normalizeAttributionProjection({...raw, items: [{malformed: true}]}),
            null,
            "malformed items fail closed"
        );
        assert.deepEqual(
            normalizeAttributionProjection({
                ...raw,
                items: [raw.items[0], {malformed: true}],
            }).items,
            normalized.items,
            "one malformed optional item cannot erase valid touchpoints"
        );

        const occurredAt = attributionOccurredAt("2026-08-25 14:30:00");
        assert.ok(occurredAt.label, "the user-facing local time remains available");
        assert.ok(
            /^2026-08-25T14:30:00(?:\.000)?Z$/.test(occurredAt.datetime),
            "the HTML datetime attribute is a valid UTC ISO-8601 instant"
        );
        assert.deepEqual(attributionOccurredAt("invalid"), {datetime: "", label: ""});
    });

    QUnit.test(
        "shows attribution loading and failure states only when useful",
        (assert) => {
            assert.notOk(
                attributionProjectionVisible({
                    enabled: false,
                    phase: "idle",
                    items: [],
                }),
                "an idle disabled projection stays hidden"
            );
            assert.ok(
                attributionProjectionVisible({
                    enabled: false,
                    phase: "loading",
                    items: [],
                }),
                "the first loading state is visible"
            );
            assert.ok(
                attributionProjectionVisible({
                    enabled: false,
                    phase: "error",
                    items: [],
                }),
                "the first failure and retry are visible"
            );
            assert.notOk(
                attributionProjectionVisible({
                    enabled: true,
                    phase: "ready",
                    items: [],
                }),
                "an enabled but empty projection stays compact"
            );
            assert.ok(
                attributionProjectionVisible({
                    enabled: true,
                    phase: "ready",
                    items: [
                        attributionItem(
                            "aaaaaaaa-1111-4111-8111-111111111111",
                            "Campanha"
                        ),
                    ],
                }),
                "a populated projection is visible"
            );
        }
    );

    QUnit.test(
        "filters malformed conversation rows before selecting the first item",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            const valid = {
                channel_id: 42,
                conversation_type: "direct",
                name: "Renderable",
            };
            store.applyConversationPage(
                {
                    items: [null, [], {}, {channel_id: "41"}, valid, {...valid}],
                    has_more: false,
                    total: 6,
                },
                {reset: true, silent: false, previousConversation: false}
            );
            assert.deepEqual(
                store.state.conversations,
                [valid],
                "duplicate channel IDs keep the first safe projection"
            );
            let selected = false;
            store.selectConversation = async (channelId, options) => {
                selected = {channelId, options};
                store.state.selectedChannelId = channelId;
                return true;
            };
            await store.reconcileConversationSelection({
                reset: true,
                silent: false,
                selectFirst: true,
                previousSelected: false,
            });
            assert.deepEqual(selected, {
                channelId: 42,
                options: {preservePane: true},
            });
            store.state.conversations.unshift(null);
            assert.strictEqual(store.selectedConversation.channel_id, 42);
        }
    );

    QUnit.test(
        "loads responsibility scopes globally and keeps selection inside them",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {user: {id: 7}};
            const other = {
                channel_id: 10,
                responsible: {id: 8, name: "Ana"},
            };
            const mine = {
                channel_id: 20,
                responsible: {id: 7, name: "Lucas"},
            };
            const unassigned = {channel_id: 30, responsible: false};
            let serverConversations = [other, mine, unassigned];
            const requestedFilters = [];
            store.call = async (_method, _args, kwargs) => {
                requestedFilters.push(kwargs.filters);
                const scope = kwargs.filters.responsibility || "all";
                const items = filterConversationsByResponsibility(
                    serverConversations,
                    scope,
                    7
                );
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items,
                    has_more: false,
                    next_cursor: false,
                    total: items.length,
                };
            };
            store.state.conversations = [...serverConversations];
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.messages = [{message_id: 1}];
            const selections = [];
            store.selectConversation = async (channelId, options) => {
                selections.push({channelId, options});
                store.state.selectedChannelId = channelId;
                return true;
            };

            await store.setFilter("responsibility", "mine");
            assert.strictEqual(store.state.selectedChannelId, 20);
            assert.deepEqual(selections.pop(), {
                channelId: 20,
                options: {preservePane: true},
            });
            assert.deepEqual(store.responsibilityVisibleConversations, [mine]);
            assert.deepEqual(requestedFilters.pop(), {
                states: ["open"],
                responsibility: "mine",
            });

            await store.setFilter("responsibility", "unassigned");
            assert.strictEqual(store.state.selectedChannelId, 30);
            assert.deepEqual(selections.pop(), {
                channelId: 30,
                options: {preservePane: true},
            });
            assert.notOk(store.setFilter("responsibility", "unexpected"));
            assert.strictEqual(store.state.filters.responsibility, "unassigned");

            serverConversations = [other];
            store.state.detailsOpen = true;
            store.state.mobilePane = "conversation";
            await store.setFilter("responsibility", "unassigned");
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.strictEqual(store.state.timelineChannelId, false);
            assert.deepEqual(store.state.messages, []);
            assert.notOk(store.state.detailsOpen);
            assert.strictEqual(store.state.mobilePane, "list");
            assert.deepEqual(
                store.conversationFilters(),
                {states: ["open"], responsibility: "unassigned"},
                "the responsibility scope is part of the paginated server contract"
            );

            serverConversations = [other, mine];
            store.state.filters.responsibility = "mine";
            assert.ok(await store.loadConversations({reset: true, selectFirst: true}));
            assert.strictEqual(
                store.state.selectedChannelId,
                20,
                "server scopes auto-select only a matching conversation"
            );
            assert.strictEqual(store.state.conversationTotal, 1);
        }
    );

    QUnit.test(
        "closes a selected conversation reassigned outside the active scope",
        (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {user: {id: 7}};
            store.state.filters.responsibility = "mine";
            store.state.conversations = [
                openConversation({
                    channel_id: 20,
                    responsible: {id: 7, name: "Lucas"},
                }),
            ];
            store.state.selectedChannelId = 20;
            store.state.timelineChannelId = 20;
            store.state.messages = [{message_id: 1}];
            store.state.detailsOpen = true;
            store.state.mobilePane = "conversation";

            assert.ok(
                store.replaceConversation({
                    channel_id: 20,
                    state: "open",
                    responsible: {id: 8, name: "Ana"},
                })
            );
            assert.strictEqual(store.state.selectedChannelId, false);
            assert.strictEqual(store.state.timelineChannelId, false);
            assert.deepEqual(store.state.messages, []);
            assert.notOk(store.state.detailsOpen);
            assert.strictEqual(store.state.mobilePane, "list");
        }
    );

    QUnit.test(
        "silent responsibility refresh never preserves a stale selected row",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {user: {id: 7}};
            store.state.filters.responsibility = "mine";
            const stale = {
                channel_id: 20,
                responsible: {id: 7, name: "Lucas"},
            };
            const next = {
                channel_id: 30,
                responsible: {id: 7, name: "Lucas"},
            };
            store.state.conversations = [stale];
            store.state.selectedChannelId = 20;
            const selections = [];
            store.selectConversation = async (channelId, options) => {
                selections.push({channelId, options});
                store.state.selectedChannelId = channelId;
                return true;
            };

            store.applyConversationPage(
                {items: [next], has_more: false, total: 1},
                {reset: true, silent: true, previousConversation: stale}
            );
            await store.reconcileConversationSelection({
                reset: true,
                silent: true,
                selectFirst: false,
                previousSelected: 20,
            });

            assert.deepEqual(store.state.conversations, [next]);
            assert.deepEqual(selections, [
                {channelId: 30, options: {preservePane: true}},
            ]);
        }
    );

    QUnit.test(
        "orders imported history by date with ID only breaking equal dates",
        (assert) => {
            const current = {message_id: 10, date: "2026-09-08 12:00:00"};
            const earlier = {message_id: 30, date: "2026-09-07 12:00:00"};
            const sameDate = {message_id: 20, date: "2026-09-08 12:00:00"};
            assert.deepEqual(
                mergeTimelineItems([current], [sameDate, earlier]).map(
                    (item) => item.message_id
                ),
                [30, 10, 20]
            );
            assert.strictEqual(
                timelineScrollDecision({
                    channelChanged: false,
                    phase: "ready",
                    preserveScroll: false,
                    wasNearBottom: true,
                    lastMessageId: 10,
                    previousLastMessageId: 30,
                    lastMessage: current,
                    previousLastMessage: earlier,
                }),
                "follow",
                "a later chronological tail can have a smaller ID"
            );
        }
    );

    QUnit.test("keeps untrusted message bodies as inert strings", (assert) => {
        const malicious = '<img src=x onerror="globalThis.pwned=true">';
        let coerced = false;
        const timeline = mergeTimelineItems(
            [],
            [
                {message_id: 7, body_text: malicious},
                {
                    message_id: 8,
                    body_text: {
                        toString() {
                            coerced = true;
                            return malicious;
                        },
                    },
                },
            ]
        );

        assert.strictEqual(timeline[0].body_text, malicious);
        assert.strictEqual(typeof timeline[0].body_text, "string");
        assert.strictEqual(timeline[1].body_text, "");
        assert.notOk(coerced, "objects are never coerced into renderable text");
    });

    QUnit.test("normalizes media, reactions and actions defensively", (assert) => {
        const [message] = mergeTimelineItems(
            [],
            [
                {
                    message_id: 41,
                    body_text: "Legenda",
                    edited_at: "2026-08-21 12:30:00",
                    media: [
                        {
                            id: 9,
                            kind: "image",
                            state: "ready",
                            name: "painel.jpg",
                            size_bytes: 2048,
                            duration_seconds: 12.5,
                            width: 1200,
                            height: 1600,
                            content_url: "/contact_center/media/9/content",
                            download_url: "https://provider.invalid/public.jpg",
                        },
                        null,
                    ],
                    reactions: [
                        {emoji: "👍", count: 2, reacted_by_me: true},
                        {emoji: "", count: 1},
                    ],
                    actions: {
                        reply: true,
                        react: true,
                        edit: false,
                        delete: true,
                        resend: true,
                    },
                    dispatch_reason: "permanent_failure",
                    retry_of_message_id: 39,
                    source_inbox_event_id: 345,
                    is_forwarded: true,
                },
            ]
        );

        assert.strictEqual(message.media.length, 1);
        assert.strictEqual(
            message.media[0].content_url,
            "/contact_center/media/9/content"
        );
        assert.strictEqual(
            message.media[0].download_url,
            "",
            "absolute provider URLs never reach a renderable href"
        );
        assert.strictEqual(
            message.media[0].duration_seconds,
            12.5,
            "known duration is preserved for the compact media player"
        );
        assert.strictEqual(message.media[0].width, 1200);
        assert.strictEqual(message.media[0].height, 1600);
        assert.deepEqual(message.reactions, [
            {emoji: "👍", count: 2, reacted_by_me: true},
        ]);
        assert.deepEqual(message.actions, {
            reply: true,
            react: true,
            edit: false,
            delete: true,
            resend: true,
        });
        assert.strictEqual(message.dispatch_reason, "permanent_failure");
        assert.strictEqual(message.retry_of_message_id, 39);
        assert.strictEqual(message.source_inbox_event_id, 345);
        assert.strictEqual(message.is_forwarded, true);
        assert.ok(isForwardedTimelineMessage(message));
        const [deliveryUpdate] = mergeTimelineItems(
            [message],
            [{message_id: 41, delivery_state: "read"}]
        );
        assert.strictEqual(deliveryUpdate.media.length, 1);
        assert.strictEqual(deliveryUpdate.reactions[0].emoji, "👍");
        assert.strictEqual(
            deliveryUpdate.edited_at,
            "2026-08-21 12:30:00",
            "partial realtime updates preserve message projections"
        );

        const [deleted] = mergeTimelineItems(
            [],
            [
                {
                    message_id: 42,
                    is_deleted: true,
                    actions: {
                        reply: true,
                        react: true,
                        edit: true,
                        delete: true,
                        resend: true,
                    },
                },
            ]
        );
        assert.deepEqual(deleted.actions, {
            reply: false,
            react: false,
            edit: false,
            delete: false,
            resend: false,
        });
        assert.notOk(
            deleted.source_inbox_event_id,
            "a missing source event fails closed"
        );
        assert.notOk(
            isForwardedTimelineMessage({...deleted, is_forwarded: true}),
            "deleted messages never expose the forwarded marker"
        );
        const [untrustedForwarded] = mergeTimelineItems(
            [],
            [{message_id: 43, is_forwarded: 1}]
        );
        assert.notOk(untrustedForwarded.is_forwarded);
        assert.strictEqual(messagePreviewText(deleted), "Mensagem apagada");
    });

    QUnit.test(
        "deleted content visibility is strict, fail-closed and preserves strike mode",
        (assert) => {
            const sensitive = {
                is_deleted: true,
                body_text: "Conteúdo apagado",
                is_forwarded: true,
                reply_to: {message_id: 9, body_text: "Mensagem citada"},
                media: [
                    {
                        id: 11,
                        kind: "image",
                        state: "ready",
                        name: "sensitive.png",
                        content_url: "/contact_center/media/11/content",
                        download_url: "/contact_center/media/11/content?download=1",
                    },
                ],
                reactions: [{emoji: "👍", count: 2, reacted_by_me: true}],
                actions: {
                    reply: true,
                    react: true,
                    edit: true,
                    delete: true,
                    resend: true,
                },
            };
            const untrustedValues = [false, null, 1, "true", {}, []];
            for (const [index, value] of untrustedValues.entries()) {
                const [redacted] = mergeTimelineItems(
                    [],
                    [
                        {
                            ...sensitive,
                            message_id: 100 + index,
                            deleted_content_visible: value,
                        },
                    ]
                );
                assert.strictEqual(
                    redacted.deleted_content_visible,
                    false,
                    `visibility ${String(value)} fails closed`
                );
                assert.strictEqual(redacted.body_text, "");
                assert.deepEqual(redacted.media, []);
                assert.deepEqual(redacted.reactions, []);
                assert.strictEqual(redacted.reply_to, false);
                assert.deepEqual(redacted.actions, {
                    reply: false,
                    react: false,
                    edit: false,
                    delete: false,
                    resend: false,
                });
            }

            const [preserved] = mergeTimelineItems(
                [],
                [
                    {
                        ...sensitive,
                        message_id: 120,
                        deleted_content_visible: true,
                    },
                ]
            );
            assert.strictEqual(preserved.deleted_content_visible, true);
            assert.strictEqual(preserved.body_text, "Conteúdo apagado");
            assert.strictEqual(preserved.media.length, 1);
            assert.strictEqual(preserved.media[0].id, 11);
            assert.strictEqual(preserved.reactions.length, 1);
            assert.strictEqual(preserved.reactions[0].emoji, "👍");
            assert.strictEqual(preserved.reply_to.message_id, 9);
            assert.deepEqual(preserved.actions, {
                reply: false,
                react: false,
                edit: false,
                delete: false,
                resend: false,
            });
        }
    );

    QUnit.test("normalizes and gates source webhook links fail-closed", (assert) => {
        const capabilities = {view_source_webhook: true};
        assert.ok(
            sourceWebhookActionEnabled(capabilities, {
                source_inbox_event_id: 345,
            })
        );
        assert.ok(
            sourceWebhookActionEnabled(capabilities, {
                source_inbox_event_id: "345",
            }),
            "a canonical numeric string is normalized by the shared ID contract"
        );
        for (const sourceInboxEventId of [false, 0, -1, 1.5, "345x", null]) {
            assert.notOk(
                sourceWebhookActionEnabled(capabilities, {
                    source_inbox_event_id: sourceInboxEventId,
                }),
                `invalid source event ${String(sourceInboxEventId)} is rejected`
            );
        }
        assert.ok(
            mergeTimelineItems(
                [],
                [false, 0, -1, 1.5, "345x", null].map((sourceInboxEventId, index) => ({
                    message_id: 500 + index,
                    source_inbox_event_id: sourceInboxEventId,
                }))
            ).every((item) => item.source_inbox_event_id === false),
            "timeline normalization collapses malformed source IDs to false"
        );
        const [normalized] = mergeTimelineItems(
            [],
            [{message_id: 510, source_inbox_event_id: "345"}]
        );
        assert.strictEqual(
            normalized.source_inbox_event_id,
            345,
            "canonical numeric strings normalize to numeric event IDs"
        );
        assert.notOk(
            sourceWebhookActionEnabled(
                {view_source_webhook: 1},
                {source_inbox_event_id: 345}
            ),
            "only the exact bootstrap capability boolean grants access"
        );
    });

    QUnit.test(
        "allow-lists group delivery counts without participant identifiers",
        (assert) => {
            const [message] = mergeTimelineItems(
                [],
                [
                    {
                        message_id: 57,
                        group_delivery: {
                            delivered_count: 5,
                            read_count: 3,
                            participant_total: 260,
                            participant_jids: ["5511999999999@s.whatsapp.net"],
                            participant_lids: ["123456789@lid"],
                        },
                    },
                ]
            );

            assert.deepEqual(message.group_delivery, {
                delivered_count: 5,
                read_count: 3,
            });
            assert.strictEqual(groupDeliveryLabel(message), "3 leram · 5 receberam");
            assert.strictEqual(
                JSON.stringify(message.group_delivery),
                '{"delivered_count":5,"read_count":3}',
                "the renderable summary has no roster, JID or LID field"
            );
            assert.strictEqual(
                groupDeliveryLabel({
                    group_delivery: {delivered_count: 1, read_count: 1},
                }),
                "1 leu · 1 recebeu"
            );
            for (const malformed of [
                null,
                {delivered_count: "5", read_count: 3},
                {delivered_count: 5, read_count: -1},
                {delivered_count: 5},
            ]) {
                const [invalid] = mergeTimelineItems(
                    [],
                    [{message_id: 58, group_delivery: malformed}]
                );
                assert.notOk(invalid.group_delivery);
                assert.strictEqual(groupDeliveryLabel(invalid), "");
            }
        }
    );

    QUnit.test("validates composer files against provider capabilities", (assert) => {
        const capabilities = {
            media: {
                image: {
                    enabled: true,
                    max_bytes: 4096,
                    mimetypes: ["image/jpeg"],
                },
                document: true,
            },
        };

        assert.deepEqual(Object.keys(mediaCapabilities(capabilities)), [
            "image",
            "document",
        ]);
        for (const invalidConfig of [
            {enabled: "false"},
            {enabled: 0},
            {enabled: null},
            {max_bytes: "4096"},
            {max_bytes: false},
            {caption: "false"},
            {mimetypes: "image/jpeg"},
            {mimetypes: ["image/jpeg", ""]},
            {recording_mimetypes: ["audio/mp4"]},
            {
                recording_mimetypes: ["audio/mp4"],
                voice_note_mimetypes: [],
                max_duration_seconds: "900",
            },
        ]) {
            assert.deepEqual(
                mediaCapabilities({media: {image: invalidConfig}}),
                {},
                "malformed structured media capabilities fail closed"
            );
        }
        assert.deepEqual(
            mediaCapabilities({
                media: {
                    audio: {
                        enabled: true,
                        recording_mimetypes: ["audio/mp4"],
                        voice_note_mimetypes: [],
                        max_duration_seconds: 900,
                    },
                },
            }).audio.recording_mimetypes,
            ["audio/mp4"],
            "the provider recorder contract survives capability normalization"
        );
        assert.notOk(
            mediaCaptionAllowed(
                {media: {audio: {enabled: true, caption: false}}},
                "audio"
            ),
            "an explicit provider caption restriction is preserved"
        );
        assert.ok(
            mediaCaptionAllowed({media: {image: {enabled: true}}}, "image"),
            "caption support defaults to true for compatible providers"
        );
        assert.deepEqual(
            validateMediaFile(
                {name: "painel.jpg", type: "image/jpeg", size: 2048},
                capabilities
            ),
            {ok: true, kind: "image", max_bytes: 4096}
        );
        assert.strictEqual(
            validateMediaFile(
                {name: "painel.png", type: "image/png", size: 2048},
                capabilities
            ).error,
            "O formato deste arquivo não é aceito pelo provedor."
        );
        assert.strictEqual(
            validateMediaFile(
                {name: "painel.jpg", type: "image/jpeg", size: 4097},
                capabilities
            ).error,
            "O arquivo excede o limite de 4.0 KB."
        );
        assert.strictEqual(formatFileSize(1536), "1.5 KB");
        assert.ok(
            validateMediaFile(
                {
                    name: "manual.pdf",
                    type: "application/pdf",
                    size: 20 * 1024 * 1024,
                },
                {media: {document: {enabled: true}}}
            ).ok,
            "generic document limits match the backend 50 MB contract"
        );
        assert.strictEqual(
            messagePreviewText({media: [{kind: "audio", is_voice_note: true}]}),
            "Mensagem de voz"
        );
        assert.strictEqual(
            conversationPreviewText({last_message: {media: [{kind: "image"}]}}),
            "Imagem",
            "the conversation list never exposes the technical content_type"
        );
        assert.strictEqual(
            conversationPreviewText({last_message: {is_deleted: true}}),
            "Mensagem apagada"
        );
        assert.strictEqual(
            conversationPreviewText({
                conversation_type: "group",
                last_message: {
                    body_text: "Cheguei",
                    direction: "inbound",
                    author: {name: "Ana"},
                },
            }),
            "Ana: Cheguei",
            "group previews identify the participant"
        );
        assert.strictEqual(conversationPreviewText({}), "Conversa iniciada");
    });

    QUnit.test("deduplicates, merges and sorts timeline items", (assert) => {
        const existing = [
            {message_id: 9, body_text: "Nine", delivery_state: "queued"},
            {message_id: 2, body_text: "Two", delivery_state: "delivered"},
        ];
        const incoming = [
            {message_id: "9", delivery_state: "sent"},
            {message_id: 12, body_text: "Twelve"},
            {message_id: 0, body_text: "Invalid"},
            {message_id: true, body_text: "Invalid bool"},
            null,
        ];

        const result = mergeTimelineItems(existing, incoming);

        assert.deepEqual(
            result.map((item) => item.message_id),
            [2, 9, 12]
        );
        assert.strictEqual(result[1].body_text, "Nine", "partial updates keep body");
        assert.strictEqual(result[1].delivery_state, "sent", "incoming state wins");
        assert.strictEqual(
            existing[0].delivery_state,
            "queued",
            "input is not mutated"
        );
    });

    QUnit.test(
        "preserves fresher existing data when prepending older pages",
        (assert) => {
            const existing = [
                {message_id: 20, body_text: "Current", delivery_state: "read"},
            ];
            const olderPage = [
                {message_id: 10, body_text: "Older"},
                {message_id: 20, body_text: "Stale", delivery_state: "sent"},
            ];

            const result = mergeTimelineItems(existing, olderPage, {prepend: true});

            assert.deepEqual(
                result.map((item) => item.message_id),
                [10, 20]
            );
            assert.strictEqual(result[1].body_text, "Current");
            assert.strictEqual(result[1].delivery_state, "read");
        }
    );

    QUnit.test("provides monotonic delivery ranks and safe labels", (assert) => {
        assert.deepEqual(
            ["queued", "failed", "sent", "delivered", "read"].map(
                (state) => deliveryMeta(state).rank
            ),
            [0, 1, 2, 3, 4]
        );
        assert.strictEqual(deliveryMeta(" DELIVERED ").label, "Entregue");
        assert.deepEqual(
            ["queued", "sent", "delivered", "read", "failed"].map(
                (state) => deliveryMeta(state).icon
            ),
            ["○", "✓", "✓✓", "✓✓", "!"]
        );
        assert.strictEqual(deliveryMeta(false).label, "");
        assert.deepEqual(deliveryMeta("<script>"), {
            state: "unknown",
            rank: -1,
            label: "Status desconhecido",
            tone: "warning",
            icon: "○",
        });
        assert.strictEqual(
            messageStatusMeta("queued", "uncertain").label,
            "Envio incerto — não reenviar"
        );
        assert.strictEqual(messageStatusMeta("queued", "uncertain").state, "uncertain");
        assert.strictEqual(messageStatusMeta("queued", "dead").icon, "!");
        assert.strictEqual(messageStatusMeta("sent", "done").label, "Enviada");
        assert.strictEqual(
            messageStatusMeta("delivered", "uncertain").state,
            "delivered",
            "positive provider evidence overrides a stale ambiguous dispatch"
        );
        assert.strictEqual(
            messageStatusMeta("read", "dead").state,
            "read",
            "positive provider evidence also overrides a stale local failure"
        );
        const timeline = {delivery: ConversationTimeline.prototype.delivery};
        assert.strictEqual(
            ConversationTimeline.prototype.dispatchReason.call(timeline, {
                delivery_state: "delivered",
                dispatch_state: "uncertain",
                dispatch_reason: "uncertain",
            }).label,
            "",
            "a stale dispatch reason is hidden once provider delivery is proven"
        );
        assert.strictEqual(
            messageStatusMeta("queued", "blocked").state,
            "queued",
            "inbox-only blocked is never interpreted as an outbox state"
        );
        assert.strictEqual(
            messageStatusMeta("failed", "unsupported").state,
            "failed",
            "inbox-only unsupported is never interpreted as an outbox state"
        );
    });

    QUnit.test(
        "allow-lists translated outbox reasons and rejects provider text",
        (assert) => {
            const reasons = [
                "waiting_queue",
                "waiting_connection",
                "waiting_provider_limit",
                "sending",
                "automatic_retry",
                "permanent_failure",
                "uncertain",
                "cancelled",
                "retry_created",
                "retry_capability_unavailable",
                "retry_connection_unavailable",
                "retry_content_unavailable",
                "retry_reply_unavailable",
                "retry_media_unavailable",
            ];
            for (const reason of reasons) {
                const meta = messageDispatchReasonMeta(reason);
                assert.strictEqual(meta.code, reason);
                assert.ok(meta.label, `${reason} has an extracted UI label`);
            }
            assert.deepEqual(messageDispatchReasonMeta("provider said <script>"), {
                code: "",
                label: "",
                tone: "muted",
            });

            const [normalized] = mergeTimelineItems(
                [],
                [
                    {
                        message_id: 701,
                        dispatch_reason: "provider said <script>",
                        retry_of_message_id: "not-an-id",
                        actions: {resend: "true"},
                    },
                ]
            );
            assert.strictEqual(normalized.dispatch_reason, "");
            assert.strictEqual(normalized.retry_of_message_id, false);
            assert.strictEqual(normalized.actions.resend, false);
        }
    );

    QUnit.test("maps every realtime state to an explicit status", (assert) => {
        assert.deepEqual(realtimeStatusMeta("online"), {
            state: "online",
            label: "Tempo real ativo",
            description: "Atualizações em tempo real ativas",
        });
        assert.deepEqual(realtimeStatusMeta("connecting"), {
            state: "connecting",
            label: "Conectando…",
            description: "Conectando às atualizações em tempo real",
        });
        assert.deepEqual(realtimeStatusMeta("offline"), {
            state: "offline",
            label: "Sem tempo real",
            description: "Atualizações em tempo real indisponíveis",
        });
        assert.strictEqual(
            realtimeStatusMeta("unexpected").state,
            "connecting",
            "unknown transient states fail safely"
        );
    });

    QUnit.test(
        "normalizes and summarizes provider-neutral connection health",
        (assert) => {
            const health = normalizeConnectionHealth({
                summary: {
                    total: 999,
                    connected: 999,
                    last_check_at: "2026-08-23 10:00:00",
                },
                items: [
                    {
                        id: 10,
                        account_id: 4,
                        account_name: "Comercial 01",
                        connection_name: "WuzAPI Comercial 01",
                        state: "connected",
                        last_check_at: "2026-08-23 09:58:00",
                        can_check: true,
                    },
                    {
                        id: 20,
                        account_id: 5,
                        account_name: "Comercial 02",
                        connection_name: "WuzAPI Comercial 02",
                        display_address: "+55 11 99999-0002",
                        platform: "whatsapp",
                        provider: "wuzapi",
                        connection_role: "primary",
                        accepts_inbound: true,
                        unsupported_count: 7,
                        state: "authentication_required",
                        detail: "authentication_required",
                        last_check_at: "2026-08-23 09:55:00",
                        can_check: true,
                    },
                    {id: 30, account_name: "Comercial 03", state: "unexpected"},
                    {
                        id: 10,
                        account_name: "Comercial 01",
                        state: "degraded",
                        checking: true,
                    },
                    {id: false, state: "connected"},
                ],
            });

            assert.deepEqual(
                health.items.map((item) => [item.id, item.state]),
                [
                    [20, "authentication_required"],
                    [10, "degraded"],
                    [30, "unknown"],
                ],
                "duplicates are replaced and attention states sort before healthy ones"
            );
            assert.strictEqual(
                health.summary.total,
                3,
                "counts are derived from safe items"
            );
            assert.strictEqual(health.summary.authentication_required, 1);
            assert.strictEqual(health.summary.degraded, 1);
            assert.strictEqual(health.summary.unknown, 1);
            assert.strictEqual(health.summary.connected, 0);
            assert.strictEqual(
                health.summary.checking,
                1,
                "checking is transverse to the last-known state"
            );
            assert.strictEqual(health.summary.can_check, true);
            assert.strictEqual(health.summary.last_check_at, "2026-08-23 10:00:00");
            assert.strictEqual(health.items[0].account_name, "Comercial 02");
            assert.strictEqual(health.items[0].connection_name, "WuzAPI Comercial 02");
            assert.strictEqual(health.items[0].display_address, "+55 11 99999-0002");
            assert.strictEqual(health.items[0].platform, "whatsapp");
            assert.strictEqual(health.items[0].connection_role, "primary");
            assert.strictEqual(health.items[0].accepts_inbound, true);
            assert.strictEqual(health.items[0].unsupported_count, 7);
            assert.strictEqual(
                connectionHealthDetail(health.items[0]),
                "Faça login novamente para restabelecer a sessão."
            );
            assert.strictEqual(
                connectionHealthStateMeta("logged_out").label,
                "Login necessário",
                "transport-specific states do not leak into UI vocabulary"
            );
            assert.deepEqual(connectionFleetMeta(health), {
                state: "authentication_required",
                label: "0/3 conectadas · verificando 1",
                description: "A sessão precisa ser autenticada novamente.",
            });
            assert.strictEqual(
                connectionHealthDetail({
                    id: 99,
                    state: "unknown",
                    checking: true,
                    detail: "healthy",
                }),
                "Ainda não há uma verificação recente desta conexão.",
                "stale state takes precedence over an older healthy detail"
            );
            assert.strictEqual(
                connectionHealthDetail({
                    id: 100,
                    state: "degraded",
                    detail: "identity_mismatch",
                }),
                "O ativo conectado não corresponde ao configurado.",
                "an asset mismatch remains provider-neutral and actionable"
            );
            assert.strictEqual(normalizeConnectionHealth(null), false);

            assert.deepEqual(
                connectionFleetMeta({
                    items: [
                        {id: 1, state: "connected", checking: true},
                        {id: 2, state: "connected", checking: true},
                    ],
                }),
                {
                    state: "checking",
                    label: "2/2 conectadas · verificando 2",
                    description: "Uma verificação de saúde está em andamento.",
                },
                "checking preserves the last-known connected count"
            );
        }
    );

    QUnit.test(
        "keeps metadata-limited Meta messaging out of operational attention",
        (assert) => {
            const health = normalizeConnectionHealth({
                items: [
                    {
                        id: 60,
                        connection_name: "Meta Messenger - Soloz Industrial",
                        platform: "messenger",
                        provider: "meta",
                        state: "connected",
                        detail: "metadata_limited",
                    },
                    {
                        id: 70,
                        connection_name: "Meta Instagram - @solozindustrial",
                        platform: "instagram",
                        provider: "meta",
                        state: "connected",
                        detail: "metadata_limited",
                    },
                    {
                        id: 80,
                        connection_name: "WuzAPI Comercial",
                        platform: "whatsapp",
                        provider: "wuzapi",
                        state: "connected",
                        detail: "healthy",
                    },
                    {
                        id: 90,
                        account_name: "teste",
                        connection_name: "teste",
                        platform: "whatsapp",
                        provider: "wuzapi",
                        connection_role: "migration",
                        state: "authentication_required",
                        detail: "authentication_required",
                    },
                ],
            });

            assert.strictEqual(health.summary.connected, 3);
            assert.strictEqual(health.summary.total, 4);
            assert.strictEqual(health.summary.advisory, 2);
            assert.deepEqual(connectionFleetMeta(health), {
                state: "authentication_required",
                label: "3/4 conectadas",
                description: "A sessão precisa ser autenticada novamente.",
            });
            assert.deepEqual(
                connectionFleetMeta({
                    items: health.items.filter((item) => item.id !== 90),
                }),
                {
                    state: "connected",
                    label: "3/3 conectadas",
                    description:
                        "2 conexões operacionais com metadados opcionais limitados.",
                },
                "optional metadata never degrades an operational fleet"
            );
            const messenger = health.items.find((item) => item.id === 60);
            const instagram = health.items.find((item) => item.id === 70);
            const testConnection = health.items.find((item) => item.id === 90);
            assert.strictEqual(
                connectionHealthDetail(messenger),
                "Mensageria operacional; metadados do ativo não puderam ser validados."
            );
            assert.notOk(connectionHealthNeedsAttention(messenger));
            assert.notOk(connectionHealthNeedsAttention(instagram));
            assert.ok(connectionHealthNeedsAttention(testConnection));

            const component = {
                health,
                local: {view: "attention"},
            };
            const attentionCount = Object.getOwnPropertyDescriptor(
                ConnectionHealth.prototype,
                "attentionCount"
            ).get;
            const visibleItems = Object.getOwnPropertyDescriptor(
                ConnectionHealth.prototype,
                "visibleItems"
            ).get;
            Object.defineProperty(component, "attentionCount", {
                get: () => attentionCount.call(component),
            });
            assert.strictEqual(attentionCount.call(component), 1);
            assert.deepEqual(
                visibleItems.call(component).map((item) => item.id),
                [90],
                "only the connection that really requires login remains in attention"
            );
            assert.strictEqual(
                ConnectionHealth.prototype.detailLabel.call(component, messenger),
                "",
                "the connected Meta row does not render an error-like detail"
            );
        }
    );

    QUnit.test("merges one health event and recomputes its fleet summary", (assert) => {
        const current = normalizeConnectionHealth({
            items: [
                {id: 10, account_name: "A", state: "connected", can_check: true},
                {id: 20, account_name: "B", state: "connected"},
            ],
        });
        const health = mergeConnectionHealth(current, {
            id: 20,
            account_name: "B",
            state: "disconnected",
            last_check_at: "2026-08-23 10:05:00",
        });

        assert.strictEqual(health.summary.total, 2);
        assert.strictEqual(health.summary.connected, 1);
        assert.strictEqual(health.summary.disconnected, 1);
        assert.strictEqual(health.summary.can_check, true);
        assert.deepEqual(
            health.items.map((item) => item.id),
            [20, 10],
            "the problem connection moves to the actionable top of the list"
        );
    });

    QUnit.test("unknown health event requests a scoped refresh", (assert) => {
        const current = normalizeConnectionHealth({
            items: [{id: 10, account_name: "A", state: "connected"}],
        });

        assert.strictEqual(
            mergeConnectionHealth(current, {
                id: 99,
                account_name: "Outra empresa",
                state: "disconnected",
            }),
            false,
            "an ID absent from bootstrap is never inserted by an unscoped bus hint"
        );
        assert.strictEqual(current.summary.total, 1);
    });

    QUnit.test(
        "connection health disclosure closes with Escape and restores focus",
        (assert) => {
            const button = document.createElement("button");
            document.getElementById("qunit-fixture").appendChild(button);
            const state = {connectionHealthOpen: true};
            let closes = 0;
            let prevented = 0;
            let stopped = 0;
            const component = {
                state,
                store: {
                    closeConnectionHealth() {
                        closes += 1;
                        state.connectionHealthOpen = false;
                    },
                },
                toggleRef: {el: button},
                close: ConnectionHealth.prototype.close,
            };

            ConnectionHealth.prototype.onKeydown.call(component, {
                key: "Escape",
                preventDefault() {
                    prevented += 1;
                },
                stopPropagation() {
                    stopped += 1;
                },
            });

            assert.strictEqual(closes, 1);
            assert.strictEqual(prevented, 1);
            assert.strictEqual(stopped, 1);
            assert.strictEqual(document.activeElement, button);

            ConnectionHealth.prototype.onKeydown.call(component, {
                key: "Enter",
            });
            assert.strictEqual(closes, 1, "other keyboard actions remain untouched");

            const checkAllDisabled = Object.getOwnPropertyDescriptor(
                ConnectionHealth.prototype,
                "checkAllDisabled"
            ).get;
            assert.notOk(
                checkAllDisabled.call({
                    state: {connectionHealthPhase: "idle"},
                    summary: {total: 20, checking: 1},
                }),
                "a staggered fleet check does not block checking the other numbers"
            );
            assert.ok(
                checkAllDisabled.call({
                    state: {connectionHealthPhase: "idle"},
                    summary: {total: 20, checking: 20},
                }),
                "the fleet action is disabled once every number is already checking"
            );
        }
    );

    QUnit.test("localizes the supported conversation states", (assert) => {
        assert.deepEqual(conversationStateMeta("open"), {
            key: "open",
            label: "Aberta",
            tone: "progress",
        });
        assert.deepEqual(conversationStateMeta("resolved"), {
            key: "resolved",
            label: "Resolvida",
            tone: "success",
        });
        assert.deepEqual(conversationStateMeta("archived"), {
            key: "archived",
            label: "Arquivada",
            tone: "muted",
        });
        assert.deepEqual(conversationStateMeta("unexpected"), {
            key: "unexpected",
            label: "",
            tone: "muted",
        });
        assert.deepEqual(conversationStateMeta("pending"), {
            key: "pending",
            label: "",
            tone: "muted",
        });
    });

    QUnit.test("exposes one contextual open or resolved action", (assert) => {
        assert.deepEqual(conversationResolutionAction({state: "open"}), {
            target: "resolved",
            label: "Resolver",
            icon: "fa-check",
            tone: "resolve",
        });
        assert.deepEqual(conversationResolutionAction({state: "resolved"}), {
            target: "open",
            label: "Reabrir",
            icon: "fa-undo",
            tone: "reopen",
        });
        assert.deepEqual(conversationResolutionAction({state: "archived"}), {
            target: "open",
            label: "Desarquivar",
            icon: "fa-inbox",
            tone: "reopen",
        });
        assert.strictEqual(conversationResolutionAction({state: "pending"}), false);
        assert.strictEqual(conversationResolutionAction(null), false);
    });

    QUnit.test("normalizes personal pin and mute preferences fail-closed", (assert) => {
        assert.deepEqual(
            conversationPreference({
                preference: {
                    pinned: true,
                    pinned_at: "2026-09-04 12:00:00",
                    muted: true,
                },
            }),
            {
                pinned: true,
                pinned_at: "2026-09-04 12:00:00",
                muted: true,
            }
        );
        assert.deepEqual(conversationPreference({preference: {pinned: 1}}), {
            pinned: false,
            pinned_at: false,
            muted: false,
        });
        assert.deepEqual(conversationPreference(false), {
            pinned: false,
            pinned_at: false,
            muted: false,
        });
    });

    QUnit.test(
        "preserves cached tails across segmented conversation cursors",
        (assert) => {
            const pinnedCursor = {
                segment: "pinned",
                pinned_at: "2026-09-04 12:00:00",
                channel_id: 20,
            };
            assert.ok(
                conversationFollowsCursor(
                    {
                        channel_id: 30,
                        preference: {
                            pinned: true,
                            pinned_at: "2026-09-04 11:59:59",
                            muted: false,
                        },
                    },
                    pinnedCursor
                ),
                "an older pinned row follows a pinned cursor"
            );
            assert.ok(
                conversationFollowsCursor(
                    {
                        channel_id: 19,
                        preference: {
                            pinned: true,
                            pinned_at: "2026-09-04 12:00:00",
                            muted: false,
                        },
                    },
                    pinnedCursor
                ),
                "the channel id is the deterministic pinned tie-breaker"
            );
            assert.ok(
                conversationFollowsCursor(
                    {
                        channel_id: 99,
                        last_activity_at: "2026-09-04 15:00:00",
                        preference: {pinned: false, pinned_at: false, muted: false},
                    },
                    pinnedCursor
                ),
                "the activity segment always follows the pinned segment"
            );
            assert.notOk(
                conversationFollowsCursor(
                    {
                        channel_id: 21,
                        preference: {
                            pinned: true,
                            pinned_at: "2026-09-04 12:00:00",
                            muted: false,
                        },
                    },
                    pinnedCursor
                ),
                "a row before the pinned boundary is not restored"
            );

            const activityCursor = {
                segment: "activity",
                last_activity_at: "2026-09-04 10:00:00",
                channel_id: 20,
            };
            assert.ok(
                conversationFollowsCursor(
                    {
                        channel_id: 19,
                        last_activity_at: "2026-09-04 10:00:00",
                        preference: {pinned: false, pinned_at: false, muted: false},
                    },
                    activityCursor
                )
            );
            assert.notOk(
                conversationFollowsCursor(
                    {
                        channel_id: 1,
                        last_activity_at: "2026-09-04 09:00:00",
                        preference: {
                            pinned: true,
                            pinned_at: "2026-09-04 12:00:00",
                            muted: false,
                        },
                    },
                    activityCursor
                ),
                "a stale pinned row is not restored inside the activity segment"
            );
            assert.notOk(
                conversationFollowsCursor(
                    {
                        channel_id: 19,
                        last_activity_at: "2026-09-04 10:00:00",
                        preference: {pinned: false, pinned_at: false, muted: false},
                    },
                    {
                        last_activity_at: "2026-09-04 10:00:00",
                        channel_id: 20,
                    }
                ),
                "the obsolete unsegmented cursor shape fails closed"
            );
        }
    );

    QUnit.test("persists inbox density and fails closed", (assert) => {
        const values = new Map();
        const storage = {
            getItem(key) {
                return values.get(key) || null;
            },
            setItem(key, value) {
                values.set(key, value);
            },
        };
        assert.strictEqual(loadInboxDensityPreference(storage), "comfortable");
        assert.ok(saveInboxDensityPreference("compact", storage));
        assert.strictEqual(loadInboxDensityPreference(storage), "compact");
        assert.notOk(saveInboxDensityPreference("tiny", storage));

        const unavailable = {
            getItem() {
                throw new Error("unavailable");
            },
            setItem() {
                throw new Error("unavailable");
            },
        };
        assert.strictEqual(loadInboxDensityPreference(unavailable), "comfortable");
        assert.notOk(saveInboxDensityPreference("compact", unavailable));

        const store = new ContactCenterStore({
            orm: {},
            busService: {},
            notification: false,
            inboxDensityStorage: storage,
        });
        assert.strictEqual(store.state.inboxDensity, "compact");
        assert.strictEqual(store.toggleInboxDensity(), "comfortable");
        assert.strictEqual(loadInboxDensityPreference(storage), "comfortable");
    });

    QUnit.test("filters only valid Contact Center bus notifications", (assert) => {
        const accepted = {
            schema_version: SUPPORTED_SCHEMA_VERSION,
            event_type: "delivery_updated",
            channel_id: 42,
            message_id: 73,
            state: "read",
        };
        const health = {
            schema_version: SUPPORTED_SCHEMA_VERSION,
            event_type: CONNECTION_HEALTH_EVENT_TYPE,
            connection_id: 8,
            item: {id: 8, state: "connected"},
        };
        const mismatchedHealthItem = {
            ...health,
            item: {id: 9, state: "disconnected"},
        };
        const result = contactCenterNotifications([
            {type: CONTACT_CENTER_NOTIFICATION_TYPE, payload: accepted},
            {type: CONTACT_CENTER_NOTIFICATION_TYPE, payload: health},
            {
                type: CONTACT_CENTER_NOTIFICATION_TYPE,
                payload: mismatchedHealthItem,
            },
            {type: "mail.message/inbox", payload: accepted},
            {
                type: CONTACT_CENTER_NOTIFICATION_TYPE,
                payload: {...accepted, schema_version: 2},
            },
            {
                type: CONTACT_CENTER_NOTIFICATION_TYPE,
                payload: {...accepted, event_type: "<script>"},
            },
            {
                type: CONTACT_CENTER_NOTIFICATION_TYPE,
                payload: {...accepted, channel_id: "42"},
            },
            {
                type: CONTACT_CENTER_NOTIFICATION_TYPE,
                payload: {...health, connection_id: "8"},
            },
            {type: CONTACT_CENTER_NOTIFICATION_TYPE, payload: null},
            null,
        ]);

        assert.deepEqual(result, [
            accepted,
            health,
            {
                schema_version: SUPPORTED_SCHEMA_VERSION,
                event_type: CONNECTION_HEALTH_EVENT_TYPE,
                connection_id: 8,
            },
        ]);
        assert.deepEqual(contactCenterNotifications({detail: []}), []);
    });

    QUnit.test(
        "positive delivery reconciliation clears a stale dispatch warning",
        (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {
                    addEventListener: () => undefined,
                    removeEventListener: () => undefined,
                },
                notification: false,
            });
            store.state.selectedChannelId = 42;
            store.state.messages = [
                {
                    message_id: 73,
                    delivery_state: "queued",
                    dispatch_state: "uncertain",
                    dispatch_reason: "uncertain",
                },
            ];

            assert.ok(
                store.handleDeliveryNotification({
                    event_type: "delivery_updated",
                    channel_id: 42,
                    message_id: 73,
                    state: "delivered",
                    dispatch_state: "done",
                })
            );
            assert.strictEqual(store.state.messages[0].delivery_state, "delivered");
            assert.strictEqual(store.state.messages[0].dispatch_state, "done");
            assert.strictEqual(store.state.messages[0].dispatch_reason, "");
            store.destroy();
        }
    );

    QUnit.test(
        "counts preserved selected rows without understating the list",
        (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });

            store.state.conversationTotal = 1;
            store.state.conversations = [{channel_id: 10}, {channel_id: 20}];
            assert.strictEqual(store.displayedConversationTotal, 2);

            store.state.conversationTotal = 75;
            assert.strictEqual(store.displayedConversationTotal, 75);

            store.state.bootstrap = {
                states: {
                    conversation: [
                        {key: "open", label: "Aberta no servidor"},
                        {key: "resolved", label: "Resolvida no servidor"},
                    ],
                },
            };
            assert.deepEqual(
                store.conversationStates.map(({key, label, tone}) => ({
                    key,
                    label,
                    tone,
                })),
                [
                    {key: "open", label: "Aberta no servidor", tone: "progress"},
                    {
                        key: "resolved",
                        label: "Resolvida no servidor",
                        tone: "success",
                    },
                ],
                "translated server labels take precedence over local fallbacks"
            );
        }
    );

    QUnit.test(
        "refreshes the active state filter after a transition",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                inboxDensityStorage: false,
            });
            const patches = [];
            const reloads = [];
            store.state.filters.states = ["open"];
            store.state.selectedChannelId = 10;
            store.state.conversations = [
                openConversation({channel_id: 10, name: "Atendimento"}),
            ];
            store.call = async (method, args) => {
                assert.strictEqual(method, "update_conversation");
                assert.strictEqual(args[0], 10);
                patches.push(args[1]);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    item: openConversation({
                        channel_id: 10,
                        name: "Atendimento",
                        state: args[1].state,
                    }),
                };
            };
            store.loadConversations = async (options) => {
                reloads.push(options);
                return true;
            };

            assert.ok(await store.setConversationState("resolved"));
            assert.deepEqual(patches, [{state: "resolved"}]);
            assert.deepEqual(reloads, [{reset: true, selectFirst: true}]);

            store.state.filters.states = [];
            store.state.selectedChannelId = 10;
            store.state.conversations = [
                openConversation({
                    channel_id: 10,
                    name: "Atendimento",
                    state: "resolved",
                }),
            ];
            assert.ok(await store.setConversationState("open"));
            assert.deepEqual(patches, [{state: "resolved"}, {state: "open"}]);
            assert.strictEqual(
                reloads.length,
                1,
                "the all-states filter keeps the row"
            );
        }
    );

    QUnit.test(
        "state chips toggle independently and combine with OR",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            const loads = [];
            store.loadConversations = async (options) => {
                loads.push(options);
                return true;
            };
            const list = {state: store.state, store};

            assert.ok(
                await ConversationList.prototype.onStateClick.call(list, "resolved")
            );
            assert.deepEqual(store.state.filters.states, ["open", "resolved"]);
            assert.deepEqual(store.conversationFilters(), {
                states: ["open", "resolved"],
            });

            assert.ok(await ConversationList.prototype.onStateClick.call(list, "open"));
            assert.deepEqual(store.state.filters.states, ["resolved"]);

            assert.ok(
                await ConversationList.prototype.onStateClick.call(list, "resolved")
            );
            assert.deepEqual(store.state.filters.states, []);
            assert.deepEqual(
                store.conversationFilters(),
                {},
                "no selected state means all states"
            );
            assert.notOk(
                await ConversationList.prototype.onStateClick.call(list, "unsupported")
            );
            assert.deepEqual(store.state.filters.states, []);
            assert.strictEqual(loads.length, 3);
        }
    );

    QUnit.test(
        "updates personal pin and mute preferences through one contract",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                inboxDensityStorage: false,
            });
            store.state.selectedChannelId = 10;
            store.state.conversations = [
                {
                    channel_id: 10,
                    state: "open",
                    preference: {pinned: false, pinned_at: false, muted: false},
                },
            ];
            const calls = [];
            store.call = async (method, args) => {
                calls.push({method, args});
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    item: {
                        channel_id: 10,
                        state: "open",
                        preference: {
                            pinned: args[1].pinned === true,
                            pinned_at:
                                args[1].pinned === true ? "2026-09-04 12:00:00" : false,
                            muted: args[1].muted === true,
                        },
                    },
                };
            };
            const reloads = [];
            store.refreshLoadedConversations = async (options) => {
                reloads.push(options);
                return true;
            };

            assert.ok(await store.toggleConversationPinned());
            assert.deepEqual(calls[0], {
                method: "set_conversation_preference",
                args: [10, {pinned: true}],
            });
            assert.ok(store.selectedConversation.preference.pinned);
            assert.deepEqual(reloads, [{silent: true}]);
            assert.notOk(await store.setConversationPreference({muted: "yes"}));
            assert.strictEqual(
                calls.length,
                1,
                "invalid preference never reaches the RPC"
            );
        }
    );

    QUnit.test(
        "row actions mutate their loaded target without selecting or reading it",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {removeEventListener: () => undefined},
                notification: false,
            });
            store.state.filters.states = ["open"];
            store.state.conversations = [
                openConversation({
                    channel_id: 10,
                    preference: {pinned: false, muted: false},
                }),
                openConversation({
                    channel_id: 20,
                    preference: {pinned: true, muted: true},
                }),
            ];
            store.state.selectedChannelId = 20;
            store.state.timelineChannelId = 20;
            store.state.messages = [{message_id: 200}];
            store.state.replyTo = {message_id: 200};
            store.state.detailsOpen = true;
            const calls = [];
            store.call = async (method, args) => {
                calls.push({method, args});
                const target = store.loadedConversation(args[0]);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    item: {
                        ...target,
                        ...(method === "update_conversation" ? args[1] : {}),
                        preference: {...target.preference, ...args[1]},
                    },
                };
            };
            store.refreshLoadedConversations = async () => true;
            store.loadConversations = async () => {
                assert.ok(false, "a row action never opens a different conversation");
            };

            assert.ok(await store.toggleConversationPinned(10));
            assert.ok(await store.toggleConversationMuted(10));
            assert.ok(await store.setConversationState("archived", 10));
            assert.deepEqual(
                calls,
                [
                    {method: "set_conversation_preference", args: [10, {pinned: true}]},
                    {method: "set_conversation_preference", args: [10, {muted: true}]},
                    {method: "update_conversation", args: [10, {state: "archived"}]},
                ],
                "only mutation RPCs target the row; no timeline or mark_seen call"
            );
            assert.notOk(
                store.loadedConversation(10),
                "archive removes only the targeted open row"
            );
            assert.strictEqual(store.state.selectedChannelId, 20);
            assert.strictEqual(store.state.timelineChannelId, 20);
            assert.deepEqual(store.state.messages, [{message_id: 200}]);
            assert.deepEqual(store.state.replyTo, {message_id: 200});
            assert.ok(store.state.detailsOpen);
            store.destroy();
        }
    );

    QUnit.test(
        "each rendered conversation menu keeps its own row in grouped and flat lists",
        async (assert) => {
            registry.category("services").add("hotkey", hotkeyService);
            registry.category("services").add("ui", uiService);
            registry.category("services").add("dialog", {
                start: () => ({add: () => () => undefined}),
            });
            makeFakeLocalizationService();
            const env = await makeTestEnv();
            const target = getFixture();
            const store = new ContactCenterStore({
                orm: {},
                busService: new EventTarget(),
            });
            store.state.conversations = [
                openConversation({
                    channel_id: 10,
                    name: "Ana",
                    account: {id: 1, name: "Caixa", platform: "whatsapp"},
                    preference: {pinned: false, muted: false},
                }),
                openConversation({
                    channel_id: 20,
                    name: "Bruno",
                    account: {id: 1, name: "Caixa", platform: "whatsapp"},
                    state: "archived",
                    preference: {pinned: true, muted: true},
                }),
            ];
            store.state.listPhase = "ready";
            const calls = [];
            store.selectConversation = (id) => calls.push(["select", id]);
            store.toggleConversationPinned = async (id) => calls.push(["pinned", id]);
            store.toggleConversationMuted = async (id) => calls.push(["muted", id]);
            store.markConversationUnread = async (id) => calls.push(["unread", id]);
            store.setConversationState = async (state, id) => calls.push([state, id]);
            try {
                const list = await mount(ConversationList, target, {
                    env,
                    props: {state: store.state, store},
                });
                for (const view of ["grouped", "flat"]) {
                    list.setView(view);
                    await nextTick();
                    for (const [id, name, labels] of [
                        [
                            10,
                            "Ana",
                            [
                                "Arquivar conversa",
                                "Silenciar conversa",
                                "Fixar conversa",
                                "Marcar como não lida",
                            ],
                        ],
                        [
                            20,
                            "Bruno",
                            [
                                "Desarquivar conversa",
                                "Ativar notificações",
                                "Desafixar conversa",
                                "Marcar como não lida",
                            ],
                        ],
                    ]) {
                        const row = `.cc-conversation-item[data-channel-id="${id}"]`;
                        const toggler = `${row} .cc-conversation-item__menu-toggle`;
                        assert.strictEqual(
                            target
                                .querySelector(`${toggler} .sr-only`)
                                .textContent.trim(),
                            `Opções da conversa ${name}`,
                            `${view}: the accessible label belongs to its row`
                        );
                        for (const [index, label] of labels.entries()) {
                            await click(target, toggler);
                            const items = target.querySelectorAll(
                                `${row} [role="menuitem"]`
                            );
                            assert.strictEqual(items[index].textContent.trim(), label);
                            await click(items[index]);
                            await nextTick();
                        }
                    }
                }
                const expected = [
                    ["archived", 10],
                    ["muted", 10],
                    ["pinned", 10],
                    ["unread", 10],
                    ["open", 20],
                    ["muted", 20],
                    ["pinned", 20],
                    ["unread", 20],
                ];
                assert.deepEqual(calls, [...expected, ...expected]);
                assert.notOk(
                    store.state.selectedChannelId,
                    "menus never select or read a conversation"
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "conversation rows subscribe through the list component's reactive state",
        (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                stateFactory: (state) => reactive(state, () => undefined),
            });
            store.state.conversations = [
                openConversation({channel_id: 10, preference: {pinned: false}}),
                openConversation({channel_id: 20}),
            ];
            store.state.selectedChannelId = 20;
            store.state.conversationTotal = 2;
            let listUpdates = 0;
            const list = Object.create(ConversationList.prototype);
            list.props = {
                store,
                // OWL wraps a reactive prop with the child component's own callback.
                state: reactive(store.state, () => {
                    listUpdates += 1;
                }),
            };
            assert.notOk(list.filteredConversations[0].preference.pinned);
            store.replaceConversation(
                openConversation({channel_id: 10, preference: {pinned: true}})
            );
            assert.ok(
                listUpdates > 0,
                "changing an unselected row notifies the list's observer"
            );
            assert.ok(list.filteredConversations[0].preference.pinned);
            assert.strictEqual(store.state.selectedChannelId, 20);

            listUpdates = 0;
            assert.strictEqual(list.displayedCount, "2");
            store.state.conversationTotal = 3;
            assert.ok(
                listUpdates > 0,
                "the list counter also observes its own state prop"
            );
            assert.strictEqual(list.displayedCount, "3");
        }
    );

    QUnit.test("row actions keep an empty selection empty", async (assert) => {
        const store = new ContactCenterStore({
            orm: {},
            busService: {removeEventListener: () => undefined},
            notification: false,
        });
        store.state.filters.states = [];
        store.state.conversations = [openConversation({channel_id: 10})];
        store.call = async (method, args) => ({
            schema_version: SUPPORTED_SCHEMA_VERSION,
            item: {
                ...store.loadedConversation(10),
                ...(method === "update_conversation" ? args[1] : {}),
                preference: args[1],
            },
        });
        store.refreshLoadedConversations = async () => true;
        store.loadConversations = async () => {
            assert.ok(false, "no automatic selection after a row mutation");
        };
        assert.ok(await store.setConversationState("archived", 10));
        assert.ok(await store.setConversationState("open", 10));
        assert.ok(await store.toggleConversationPinned(10));
        assert.ok(await store.toggleConversationMuted(10));
        assert.notOk(store.state.selectedChannelId);
        assert.notOk(store.state.timelineChannelId);
        assert.deepEqual(store.state.messages, []);
        store.destroy();
    });

    QUnit.test(
        "row menu locks only its target and releases the lock after failure",
        async (assert) => {
            let finishPin = null;
            const calls = [];
            const list = {
                state: {
                    conversations: [
                        openConversation({channel_id: 10}),
                        openConversation({channel_id: 20, state: "archived"}),
                    ],
                },
                ui: {pendingConversationIds: {}},
                store: {
                    toggleConversationPinned(channelId) {
                        calls.push(["pinned", channelId]);
                        return new Promise((resolve) => {
                            finishPin = resolve;
                        });
                    },
                    async toggleConversationMuted(channelId) {
                        calls.push(["muted", channelId]);
                        throw new Error("Network unavailable");
                    },
                    async setConversationState(state, channelId) {
                        calls.push([state, channelId]);
                        return true;
                    },
                },
            };
            const run = (channelId, action) =>
                ConversationList.prototype.runConversationAction.call(
                    list,
                    channelId,
                    action
                );
            const pending = run(10, "pinned");
            assert.notOk(
                await run(10, "muted"),
                "a repeated click on the same row is ignored"
            );
            assert.ok(
                await run(20, "archived"),
                "another row remains usable and its current state is used"
            );
            assert.notOk(await run(99, "pinned"), "a removed row cannot act");
            finishPin(false);
            assert.notOk(await pending);
            assert.deepEqual(
                list.ui.pendingConversationIds,
                {},
                "a false RPC result releases the lock"
            );
            await assert.rejects(run(10, "muted"), /Network unavailable/);
            assert.deepEqual(
                list.ui.pendingConversationIds,
                {},
                "a rejected RPC also releases the lock"
            );
            assert.deepEqual(calls, [
                ["pinned", 10],
                ["open", 20],
                ["muted", 10],
            ]);
        }
    );

    QUnit.test(
        "a late archive response preserves the newly selected chat",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {removeEventListener: () => undefined},
                notification: false,
            });
            store.state.filters.states = ["open"];
            store.state.conversations = [
                openConversation({channel_id: 10}),
                openConversation({channel_id: 20}),
            ];
            store.state.selectedChannelId = 10;
            let resolveUpdate = null;
            store.call = () =>
                new Promise((resolve) => {
                    resolveUpdate = resolve;
                });
            store.loadConversations = async () => {
                assert.ok(false, "the late archive does not reset another chat");
            };
            const pending = store.setConversationState("archived", 10);
            store.state.selectedChannelId = 20;
            store.state.timelineChannelId = 20;
            store.state.messages = [{message_id: 200}];
            store.state.replyTo = {message_id: 200};
            resolveUpdate({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                item: openConversation({channel_id: 10, state: "archived"}),
            });
            assert.ok(await pending);
            assert.strictEqual(store.state.selectedChannelId, 20);
            assert.strictEqual(store.state.timelineChannelId, 20);
            assert.deepEqual(store.state.messages, [{message_id: 200}]);
            assert.deepEqual(store.state.replyTo, {message_id: 200});
            store.destroy();
        }
    );

    QUnit.test(
        "row mutations reject unloaded targets and mismatched responses",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {removeEventListener: () => undefined},
                notification: false,
            });
            store.state.conversations = [
                openConversation({channel_id: 10}),
                openConversation({channel_id: 20}),
            ];
            store.state.selectedChannelId = 20;
            const before = JSON.stringify(store.state.conversations);
            let calls = 0;
            store.call = async () => {
                calls += 1;
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    item: openConversation({
                        channel_id: 20,
                        state: "archived",
                        preference: {pinned: true},
                    }),
                };
            };
            for (const channelId of [false, "10", -1, 99]) {
                assert.notOk(await store.setConversationState("archived", channelId));
                assert.notOk(await store.toggleConversationPinned(channelId));
                assert.notOk(await store.toggleConversationMuted(channelId));
            }
            assert.strictEqual(calls, 0);
            assert.notOk(await store.setConversationState("archived", 10));
            assert.notOk(await store.toggleConversationPinned(10));
            assert.strictEqual(calls, 2);
            assert.strictEqual(JSON.stringify(store.state.conversations), before);
            store.call = async () => {
                throw new Error("Access denied");
            };
            assert.notOk(await store.toggleConversationMuted(10));
            assert.strictEqual(JSON.stringify(store.state.conversations), before);
            assert.strictEqual(store.state.selectedChannelId, 20);
            store.destroy();
        }
    );

    QUnit.test(
        "late list refreshes preserve the selection at response time",
        async (assert) => {
            for (const method of ["loadConversations", "refreshLoadedConversations"]) {
                const store = new ContactCenterStore({
                    orm: {},
                    busService: {removeEventListener: () => undefined},
                    notification: false,
                });
                store.state.filters.responsibility = "all";
                store.state.conversations = [
                    openConversation({channel_id: 10}),
                    openConversation({channel_id: 20}),
                ];
                store.state.selectedChannelId = 10;
                let resolveList = null;
                store.call = () =>
                    new Promise((resolve) => {
                        resolveList = resolve;
                    });
                const pending = store[method]({reset: true, silent: true});
                store.state.selectedChannelId = 20;
                store.state.timelineChannelId = 20;
                store.state.messages = [{message_id: 200}];
                store.state.replyTo = {message_id: 200};
                resolveList({
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: [openConversation({channel_id: 30})],
                    has_more: false,
                    total: 1,
                });
                assert.ok(await pending, method);
                assert.strictEqual(store.selectedConversation.channel_id, 20, method);
                assert.notOk(
                    store.loadedConversation(10),
                    "the old selection is not preserved"
                );
                assert.strictEqual(store.state.timelineChannelId, 20);
                assert.deepEqual(store.state.messages, [{message_id: 200}]);
                assert.deepEqual(store.state.replyTo, {message_id: 200});
                store.destroy();
            }
        }
    );

    QUnit.test(
        "a late reconnect refresh preserves a newer disconnect",
        async (assert) => {
            let synchronize = null;
            let releaseRefresh = null;
            const refresh = new Promise((resolve) => {
                releaseRefresh = resolve;
            });
            const store = new ContactCenterStore({
                orm: {},
                busService: {
                    removeEventListener() {
                        // This store schedules synchronization without listeners.
                    },
                },
                notification: false,
                realtimeTimer: {
                    setTimeout(callback) {
                        synchronize = callback;
                        return 1;
                    },
                    clearTimeout() {
                        // The test invokes the captured timer directly.
                    },
                },
            });
            store.refreshLoadedConversations = () => refresh;
            store.state.realtime = "online";
            store.scheduleSynchronization(true, true);
            const pending = synchronize();
            store.onDisconnect();
            releaseRefresh();
            await pending;
            assert.strictEqual(store.state.realtime, "offline");
            store.destroy();
            store.scheduleSynchronization(true, true);
            assert.strictEqual(
                store.syncTimer,
                null,
                "destroyed stores cannot reschedule"
            );
        }
    );

    QUnit.test(
        "tracks the real bus lifecycle and keeps a consistency fallback armed",
        async (assert) => {
            const listeners = new Map();
            let busStarts = 0;
            const busService = {
                addEventListener(type, callback) {
                    const callbacks = listeners.get(type) || new Set();
                    callbacks.add(callback);
                    listeners.set(type, callbacks);
                },
                removeEventListener(type, callback) {
                    const callbacks = listeners.get(type);
                    if (callbacks) {
                        callbacks.delete(callback);
                    }
                },
                start: async () => {
                    busStarts += 1;
                },
            };
            const emit = (type, detail) => {
                for (const callback of listeners.get(type) || []) {
                    // eslint-disable-next-line callback-return
                    callback({detail});
                }
            };
            const tasks = [];
            const realtimeTimer = {
                setTimeout(callback, delay) {
                    const task = {callback, delay};
                    tasks.push(task);
                    return task;
                },
                clearTimeout(task) {
                    const index = tasks.indexOf(task);
                    if (index >= 0) {
                        tasks.splice(index, 1);
                    }
                },
            };
            const synchronizations = [];
            let healthRefreshes = 0;
            const store = new ContactCenterStore({
                orm: {},
                busService,
                notification: false,
                realtimeTimer,
            });
            store.loadBootstrap = async () => {
                store.state.phase = "ready";
            };
            store.scheduleSynchronization = (reconnect, refreshTimeline) => {
                synchronizations.push({reconnect, refreshTimeline});
            };
            store.refreshConnectionHealth = () => {
                healthRefreshes += 1;
            };

            await store.start();
            assert.strictEqual(
                store.state.realtime,
                "connecting",
                "Worker initialization is not mistaken for an open WebSocket"
            );
            assert.strictEqual(tasks.length, 1, "the consistency fallback is armed");
            assert.strictEqual(tasks[0].delay, 30000, "fallback traffic stays bounded");

            emit("connect");
            assert.strictEqual(store.state.realtime, "online");
            emit("reconnecting");
            assert.strictEqual(store.state.realtime, "connecting");
            emit("disconnect");
            assert.strictEqual(store.state.realtime, "offline");

            const fallback = tasks.shift();
            fallback.callback();
            await Promise.resolve();
            assert.deepEqual(synchronizations.shift(), {
                reconnect: false,
                refreshTimeline: true,
            });
            assert.strictEqual(healthRefreshes, 1, "offline health also converges");
            assert.strictEqual(
                busStarts,
                2,
                "offline fallback restarts the bus worker"
            );
            assert.strictEqual(tasks.length, 1, "the fallback rearms itself");

            emit("reconnect");
            assert.strictEqual(store.state.realtime, "online");
            assert.deepEqual(synchronizations.shift(), {
                reconnect: true,
                refreshTimeline: true,
            });

            emit("reconnecting");
            emit("notification", [
                {
                    type: CONTACT_CENTER_NOTIFICATION_TYPE,
                    payload: {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        event_type: "message_created",
                        channel_id: 42,
                        message_id: 73,
                    },
                },
            ]);
            assert.strictEqual(
                store.state.realtime,
                "online",
                "a notification proves connectivity for a late SharedWorker client"
            );
            assert.deepEqual(synchronizations.shift(), {
                reconnect: false,
                refreshTimeline: false,
            });

            store.destroy();
            assert.strictEqual(tasks.length, 0, "destroy cancels the fallback");
            for (const type of [
                "notification",
                "connect",
                "reconnect",
                "reconnecting",
                "disconnect",
            ]) {
                assert.strictEqual(
                    (listeners.get(type) || new Set()).size,
                    0,
                    `${type} listener is removed`
                );
            }
        }
    );

    QUnit.test(
        "one shared bus notification converges every open contact center store",
        async (assert) => {
            const listeners = new Map();
            const busService = {
                addEventListener(type, callback) {
                    const callbacks = listeners.get(type) || new Set();
                    callbacks.add(callback);
                    listeners.set(type, callbacks);
                },
                removeEventListener(type, callback) {
                    const callbacks = listeners.get(type);
                    if (callbacks) {
                        callbacks.delete(callback);
                    }
                },
                start: async () => undefined,
            };
            const realtimeTimer = {
                setTimeout: () => ({timer: true}),
                clearTimeout: () => undefined,
            };
            const stores = [1, 2].map(() => {
                const store = new ContactCenterStore({
                    orm: {},
                    busService,
                    notification: false,
                    realtimeTimer,
                });
                store.loadBootstrap = async () => undefined;
                store.synchronizations = [];
                store.scheduleSynchronization = (reconnect, refreshTimeline) => {
                    store.synchronizations.push({reconnect, refreshTimeline});
                };
                return store;
            });
            await Promise.all(stores.map((store) => store.start()));

            const detail = [
                {
                    type: CONTACT_CENTER_NOTIFICATION_TYPE,
                    payload: {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        event_type: "message_created",
                        channel_id: 118,
                        message_id: 245789,
                    },
                },
            ];
            for (const callback of listeners.get("notification") || []) {
                // eslint-disable-next-line callback-return
                callback({detail});
            }

            assert.deepEqual(
                stores.map((store) => store.state.realtime),
                ["online", "online"],
                "both windows observe the open transport"
            );
            assert.deepEqual(
                stores.map((store) => store.synchronizations),
                [
                    [{reconnect: false, refreshTimeline: false}],
                    [{reconnect: false, refreshTimeline: false}],
                ],
                "both windows independently refresh their projections"
            );
            stores.forEach((store) => store.destroy());
        }
    );

    QUnit.test(
        "seen pointers never trigger a realtime resynchronization loop",
        (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            const synchronizations = [];
            store.state.selectedChannelId = 42;
            store.scheduleSynchronization = (reconnect, refreshTimeline) => {
                synchronizations.push({reconnect, refreshTimeline});
            };
            const notification = (eventType, channelId = 42, values = {}) => ({
                type: CONTACT_CENTER_NOTIFICATION_TYPE,
                payload: {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    event_type: eventType,
                    channel_id: channelId,
                    message_id: 73,
                    ...values,
                },
            });

            store.onNotification({detail: [notification("member_seen")]});
            store.onNotification({detail: [notification("member_fetched")]});
            assert.strictEqual(
                synchronizations.length,
                0,
                "read-pointer hints do not reload the timeline"
            );

            store.onNotification({detail: [notification("message_created")]});
            store.onNotification({detail: [notification("message_updated", 99)]});
            store.onNotification({detail: [notification("message_updated")]});
            store.onNotification({
                detail: [notification("conversation_preference_updated")],
            });
            store.onNotification({detail: [notification("delivery_updated")]});
            store.onNotification({
                detail: [notification("delivery_updated", 42, {refresh: true})],
            });
            assert.deepEqual(synchronizations, [
                {reconnect: false, refreshTimeline: true},
                {reconnect: false, refreshTimeline: false},
                {reconnect: false, refreshTimeline: true},
                {reconnect: false, refreshTimeline: false},
                {reconnect: false, refreshTimeline: false},
                {reconnect: false, refreshTimeline: true},
            ]);
        }
    );

    QUnit.test(
        "muted conversations keep realtime but suppress personal attention",
        (assert) => {
            const received = [];
            const attention = {
                snapshot: () => ({
                    available: true,
                    permission: "granted",
                    sound_enabled: true,
                    unseen: 0,
                }),
                setStateListener() {
                    return undefined;
                },
                receive: (payload) => received.push(payload),
            };
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                attention,
            });
            store.state.conversations = [
                {channel_id: 10, preference: {muted: true}},
                {channel_id: 20, preference: {muted: false}},
            ];

            store.handleAttentionNotification({channel_id: 10, message_id: 1});
            store.handleAttentionNotification({
                channel_id: 20,
                message_id: 2,
                personal_attention: false,
            });
            store.handleAttentionNotification({channel_id: 20, message_id: 3});

            assert.deepEqual(received, [{channel_id: 20, message_id: 3}]);
        }
    );

    QUnit.test(
        "follows the timeline only while the viewport is at its end",
        (assert) => {
            const viewport = {scrollHeight: 1000, scrollTop: 670, clientHeight: 250};
            assert.ok(timelineViewportNearBottom(viewport));
            viewport.scrollTop = 669;
            assert.notOk(timelineViewportNearBottom(viewport));

            const update = {
                channelChanged: false,
                lastMessageId: 151,
                phase: "ready",
                preserveScroll: false,
                previousLastMessageId: 150,
            };
            assert.strictEqual(
                timelineScrollDecision({...update, wasNearBottom: true}),
                "follow"
            );
            assert.strictEqual(
                timelineScrollDecision({...update, wasNearBottom: false}),
                "notify"
            );
            assert.strictEqual(
                timelineScrollDecision({
                    ...update,
                    lastMessageId: 150,
                    wasNearBottom: false,
                }),
                "preserve",
                "prepending older messages never forces the viewport to the bottom"
            );
            assert.strictEqual(
                timelineScrollDecision({
                    ...update,
                    channelChanged: true,
                    wasNearBottom: false,
                }),
                "follow",
                "opening another conversation starts at its latest message"
            );
        }
    );

    QUnit.test(
        "timeline edge paging is bounded, preserves the visible message and fences a changed conversation",
        async (assert) => {
            const fixture = getFixture();
            fixture.innerHTML = `
                <div style="height: 200px; overflow-y: auto">
                    <article data-message-id="101" style="height: 300px"></article>
                    <article data-message-id="102" style="height: 300px"></article>
                    <article data-message-id="103" style="height: 300px"></article>
                </div>`;
            const viewport = fixture.firstElementChild;
            viewport.scrollTop = 180;
            const anchor = viewport.querySelector('[data-message-id="101"]');
            const anchorTop = anchor.getBoundingClientRect().top;
            const store = new ContactCenterStore({
                orm: {},
                busService: new EventTarget(),
                notification: false,
            });
            Object.assign(store.state, {
                selectedChannelId: 10,
                timelineChannelId: 10,
                timelinePhase: "ready",
                timelineHasMore: true,
                nextBeforeMessageId: 101,
                timelineFirstUnreadMessageId: 101,
                timelineHasMoreForward: true,
                nextAfterChronologicalMessageId: 103,
                messages: [101, 102, 103].map((message_id) => ({message_id})),
            });
            const pending = [];
            store.call = (method, args, kwargs) => {
                assert.strictEqual(method, "get_timeline");
                return new Promise((resolve) => pending.push({args, kwargs, resolve}));
            };
            const timeline = Object.assign(
                Object.create(ConversationTimeline.prototype),
                {
                    props: {state: store.state, store},
                    viewportRef: {el: viewport},
                    ui: {unseenMessages: 0, awayFromLatest: true},
                    lastScrollTop: 400,
                    paginationRequest: false,
                    paginationFrame: false,
                    preserveScroll: false,
                    positioningScroll: false,
                    destroyed: false,
                    markVisibleTailSeen: () => false,
                }
            );
            const older = timeline.onViewportScroll();
            assert.strictEqual(pending.length, 1, "approaching the top loads one page");
            assert.strictEqual(pending[0].kwargs.limit, 100);
            assert.strictEqual(pending[0].kwargs.before_message_id, 101);
            timeline.onViewportScroll();
            assert.notOk(await timeline.loadNewer(), "directions cannot compete");
            assert.notOk(
                await store.loadNewerMessages(),
                "the store also guards concurrent pages"
            );
            assert.strictEqual(
                pending.length,
                1,
                "repeated scroll events share the in-flight page"
            );

            viewport.scrollTop -= 80;
            timeline.onViewportScroll();
            viewport.insertAdjacentHTML(
                "afterbegin",
                '<article data-message-id="100" style="height: 300px"></article>'
            );
            pending[0].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 10,
                items: [{message_id: 100}, {message_id: 101}],
                has_more: true,
                next_before_message_id: 100,
            });
            assert.ok(await older);
            await new Promise((resolve) => requestAnimationFrame(resolve));
            assert.strictEqual(
                anchor.getBoundingClientRect().top,
                anchorTop + 80,
                "prepending preserves the user's additional scroll while waiting"
            );
            assert.deepEqual(
                store.state.messages.map((message) => message.message_id),
                [100, 101, 102, 103],
                "overlapping pages remain deduplicated"
            );
            timeline.onViewportScroll();
            assert.strictEqual(
                pending.length,
                1,
                "restoring the position does not drain more history"
            );
            assert.strictEqual(store.state.timelineFirstUnreadMessageId, 101);

            viewport.scrollTop = viewport.scrollHeight - viewport.clientHeight - 100;
            const newer = timeline.onViewportScroll();
            assert.strictEqual(
                pending.length,
                2,
                "approaching the bottom loads the unread tail"
            );
            assert.strictEqual(pending[1].kwargs.after_chronological_message_id, 103);
            timeline.onViewportScroll();
            assert.strictEqual(pending.length, 2);
            viewport.scrollTop = viewport.scrollHeight;
            timeline.onViewportScroll();
            viewport.insertAdjacentHTML(
                "beforeend",
                '<article data-message-id="104" style="height: 40px"></article>'
            );
            pending[1].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 10,
                items: [{message_id: 104}],
                has_more_forward: false,
                next_after_chronological_message_id: 104,
                latest_received_message_id: 104,
            });
            assert.ok(await newer);
            await new Promise((resolve) => requestAnimationFrame(resolve));
            assert.notOk(timeline.ui.awayFromLatest);

            viewport.scrollTop = 100;
            const stale = timeline.onViewportScroll();
            assert.strictEqual(pending.length, 3);
            store.state.selectedChannelId = 20;
            store.state.timelineChannelId = 20;
            store.state.messages = [{message_id: 200}];
            timeline.cancelPagination();
            viewport.scrollTop = 120;
            pending[2].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 10,
                items: [{message_id: 99}],
                has_more: false,
                next_before_message_id: false,
            });
            assert.notOk(await stale);
            await new Promise((resolve) => requestAnimationFrame(resolve));
            assert.strictEqual(
                viewport.scrollTop,
                120,
                "a stale page cannot move the new conversation"
            );
            assert.deepEqual(store.state.messages, [{message_id: 200}]);
            assert.notOk(timeline.preserveScroll);
            timeline.destroyed = true;
            timeline.cancelPagination();
            store.destroy();
        }
    );

    QUnit.test(
        "conversation list automatically pages near its end without changing selection or inbox order",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: new EventTarget(),
                notification: false,
            });
            Object.assign(store.state, {
                selectedChannelId: 10,
                listPhase: "ready",
                conversationsHaveMore: true,
                nextConversationCursor: {channel_id: 10},
                conversations: [{channel_id: 10, account: {id: 1, name: "Bruna"}}],
                messages: [{message_id: 100}],
            });
            const pending = [];
            store.call = (_method, _args, kwargs) =>
                new Promise((resolve) => pending.push({kwargs, resolve}));
            const list = Object.assign(Object.create(ConversationList.prototype), {
                props: {state: store.state, store},
                lastScrollTop: 0,
                paginationPending: false,
                destroyed: false,
            });
            const viewport = {scrollTop: 150, scrollHeight: 1000, clientHeight: 300};
            list.viewportRef = {el: viewport};
            list.onListScroll({currentTarget: viewport});
            assert.strictEqual(
                pending.length,
                0,
                "scrolling away from the end does not fetch"
            );
            viewport.scrollTop = 550;
            const loading = list.onListScroll({currentTarget: viewport});
            viewport.scrollTop = 600;
            list.onListScroll({currentTarget: viewport});
            assert.strictEqual(
                pending.length,
                1,
                "one request while approaching the end"
            );
            assert.strictEqual(
                pending[0].kwargs.limit,
                50,
                "list requests stay bounded independently of history"
            );
            pending[0].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                items: [{channel_id: 20, account: {id: 2, name: "Alice"}}],
                has_more: true,
                next_cursor: {channel_id: 20},
            });
            assert.ok(await loading);
            await new Promise((resolve) => requestAnimationFrame(resolve));
            list.onListScroll({currentTarget: viewport});
            assert.strictEqual(
                pending.length,
                1,
                "completion cannot recursively load every page"
            );
            assert.strictEqual(store.state.selectedChannelId, 10);
            assert.deepEqual(store.state.messages, [{message_id: 100}]);
            assert.deepEqual(
                groupConversationsByInbox(store.state.conversations).map(
                    (group) => group.name
                ),
                ["Alice", "Bruna"]
            );
            assert.strictEqual(
                viewport.scrollTop,
                600,
                "paging does not reset list position"
            );
            store.state.listPhase = "loading";
            viewport.scrollTop = 650;
            list.onListScroll({currentTarget: viewport});
            assert.notOk(
                await store.loadMoreConversations(),
                "a filter reload cannot compete with pagination"
            );
            assert.strictEqual(pending.length, 1);
            list.destroyed = true;
            store.state.listPhase = "ready";
            assert.notOk(
                await list.loadMore(),
                "a destroyed list cannot request a page"
            );
            store.destroy();
        }
    );

    async function mountedUnreadTimeline({focused = true, hidden = false} = {}) {
        registry.category("services").add("action", {
            start: () => ({doAction: async () => undefined}),
        });
        registry.category("services").add("dialog", {
            start: () => ({add: () => () => undefined}),
        });
        makeFakeLocalizationService();
        const env = await makeTestEnv();
        const target = getFixture();
        const originalStyle = target.style.cssText;
        target.style.cssText =
            "position:fixed;top:16px;left:16px;width:600px;height:260px;overflow:hidden;z-index:10000";
        const focusDescriptor = Object.getOwnPropertyDescriptor(document, "hasFocus");
        const hiddenDescriptor = Object.getOwnPropertyDescriptor(document, "hidden");
        const visibility = {focused, hidden};
        Object.defineProperty(document, "hasFocus", {
            configurable: true,
            value: () => visibility.focused,
        });
        Object.defineProperty(document, "hidden", {
            configurable: true,
            get: () => visibility.hidden,
        });
        const store = new ContactCenterStore({
            orm: {},
            busService: new EventTarget(),
            notification: false,
            stateFactory: reactive,
        });
        const message = {
            message_id: 100,
            date: "2026-09-09 12:00:00",
            body_text: "Resposta enviada pelo celular",
            direction: "outbound",
            origin: "external_device",
            content_type: "text",
            author: {id: 17, type: "partner", name: "Agente"},
            platform: "whatsapp",
            media: [],
            reactions: [],
            actions: {},
        };
        Object.assign(store.state, {
            selectedChannelId: 10,
            timelineChannelId: 10,
            timelinePhase: "ready",
            timelineFirstUnreadMessageId: 100,
            conversations: [
                openConversation({
                    channel_id: 10,
                    unread_count: 1,
                    first_unread_message_id: 100,
                    last_message: message,
                }),
            ],
            messages: [message],
        });
        const calls = [];
        store.call = async (method, args) => {
            calls.push({method, args});
            return {channel_id: args[0], message_id: args[1]};
        };
        const timeline = await mount(ConversationTimeline, target, {
            env,
            props: {state: store.state, store},
        });
        timeline.viewportRef.el.parentElement.style.height = "240px";
        async function settle() {
            await nextTick();
            await new Promise((resolve) => requestAnimationFrame(resolve));
            await new Promise((resolve) => requestAnimationFrame(resolve));
            await nextTick();
        }
        function close() {
            timeline.__owl__.app.destroy();
            store.destroy();
            target.style.cssText = originalStyle;
            for (const [key, descriptor] of [
                ["hasFocus", focusDescriptor],
                ["hidden", hiddenDescriptor],
            ]) {
                if (descriptor) {
                    Object.defineProperty(document, key, descriptor);
                } else {
                    delete document[key];
                }
            }
        }
        return {store, timeline, target, calls, visibility, settle, close};
    }

    QUnit.test(
        "a mounted short external-device conversation is read without a scroll event",
        async (assert) => {
            const fixture = await mountedUnreadTimeline();
            const {store, timeline, target, calls, settle, close} = fixture;
            try {
                await settle();
                assert.strictEqual(timeline.viewportRef.el.scrollTop, 0);
                assert.deepEqual(calls, [{method: "mark_seen", args: [10, 100]}]);
                assert.strictEqual(store.selectedConversation.unread_count, 0);
                assert.notOk(target.querySelector(".cc-unread-divider"));
                const next = {
                    ...store.state.messages[0],
                    message_id: 101,
                    date: "2026-09-09 12:01:00",
                };
                store.selectedConversation.last_message = next;
                store.state.messages = [...store.state.messages, next];
                await settle();
                assert.deepEqual(
                    calls.map((call) => call.args[1]),
                    [100, 101],
                    "a newly visible tail is acknowledged even before its unread counter arrives"
                );
            } finally {
                close();
            }
        }
    );

    QUnit.test(
        "visible-tail observation respects unread history gaps and reacts to layout changes",
        async (assert) => {
            const fixture = await mountedUnreadTimeline({focused: false});
            const {store, timeline, target, calls, visibility, settle, close} = fixture;
            try {
                store.state.timelineHasMoreForward = true;
                await settle();
                visibility.focused = true;
                window.dispatchEvent(new Event("focus"));
                await settle();
                assert.strictEqual(
                    calls.length,
                    0,
                    "an unloaded unread tail cannot be acknowledged"
                );
                const spacer = document.createElement("div");
                spacer.style.cssText = "height:500px;min-height:500px;flex-shrink:0";
                timeline.tailRef.el.before(spacer);
                store.state.timelineHasMoreForward = false;
                timeline.viewportRef.el.scrollTop = 0;
                await settle();
                assert.strictEqual(
                    calls.length,
                    0,
                    "a long visible history still has unread content below the viewport"
                );
                assert.ok(target.querySelector(".cc-unread-divider"));
                spacer.remove();
                await settle();
                assert.strictEqual(
                    calls.length,
                    1,
                    "layout contraction exposes the tail without requiring scroll or a new message"
                );
                assert.strictEqual(store.selectedConversation.unread_count, 0);
            } finally {
                close();
            }
        }
    );

    QUnit.test(
        "a mounted timeline waits for a visible focused conversation and cleans up its observer",
        async (assert) => {
            const fixture = await mountedUnreadTimeline({focused: false, hidden: true});
            const {store, timeline, target, calls, visibility, settle, close} = fixture;
            try {
                await settle();
                assert.strictEqual(
                    calls.length,
                    0,
                    "hidden documents do not acknowledge"
                );
                visibility.hidden = false;
                document.dispatchEvent(new Event("visibilitychange"));
                await settle();
                assert.strictEqual(
                    calls.length,
                    0,
                    "a visible but unfocused window does not acknowledge"
                );
                target.style.transform = "translateX(200vw)";
                await settle();
                visibility.focused = true;
                window.dispatchEvent(new Event("focus"));
                await settle();
                assert.strictEqual(
                    calls.length,
                    0,
                    "the off-window mobile pane is not a visible conversation"
                );
                target.style.transform = "none";
                await settle();
                assert.deepEqual(calls, [{method: "mark_seen", args: [10, 100]}]);
                assert.strictEqual(store.selectedConversation.unread_count, 0);
                timeline.__owl__.app.destroy();
                assert.notOk(timeline.tailObserver);
                window.dispatchEvent(new Event("focus"));
                document.dispatchEvent(new Event("visibilitychange"));
                await settle();
                assert.strictEqual(
                    calls.length,
                    1,
                    "destroyed components cannot acknowledge again"
                );
            } finally {
                close();
            }
        }
    );

    QUnit.test(
        "opening at the first unread reconciles a clamped viewport without a scroll event",
        async (assert) => {
            const fixture = getFixture();
            for (const scenario of [
                {name: "short timeline", before: 40, after: 40, atLatest: true},
                {name: "already at the end", before: 300, after: 40, atLatest: true},
                {name: "long unread tail", before: 300, after: 600, atLatest: false},
                {
                    name: "newer history is not loaded",
                    before: 40,
                    after: 40,
                    hasMoreForward: true,
                    atLatest: false,
                },
            ]) {
                fixture.innerHTML = `
                    <div style="position: relative; height: 200px; overflow-y: auto">
                        <div class="before"></div>
                        <div data-unread-boundary="101" style="height: 20px"></div>
                        <div class="after"></div>
                    </div>`;
                const viewport = fixture.firstElementChild;
                viewport.querySelector(".before").style.height = `${scenario.before}px`;
                viewport.querySelector(".after").style.height = `${scenario.after}px`;
                viewport.scrollTop = viewport.scrollHeight;
                const initialTop = viewport.scrollTop;
                const timeline = {
                    state: {
                        selectedChannelId: 10,
                        timelineFirstUnreadMessageId: 101,
                        timelineHasMoreForward: Boolean(scenario.hasMoreForward),
                    },
                    ui: {unseenMessages: 0, awayFromLatest: false},
                    viewportRef: {el: viewport},
                    followLatest: true,
                    markVisibleTailSeen: () => false,
                    scrollToBottom: () =>
                        assert.ok(false, "the unread anchor remains present"),
                };
                ConversationTimeline.prototype.scrollToInitialPosition.call(timeline);
                await new Promise((resolve) => requestAnimationFrame(resolve));
                assert.strictEqual(
                    timeline.followLatest,
                    scenario.atLatest,
                    scenario.name
                );
                assert.strictEqual(
                    timeline.ui.awayFromLatest,
                    !scenario.atLatest,
                    scenario.name
                );
                assert.strictEqual(
                    timeline.state.timelineFirstUnreadMessageId,
                    101,
                    "positioning preserves the unread anchor"
                );
                if (scenario.atLatest) {
                    assert.strictEqual(
                        viewport.scrollTop,
                        initialTop,
                        "the no-movement case still reconciles the jump button"
                    );
                } else if (!scenario.hasMoreForward) {
                    assert.strictEqual(
                        viewport.scrollTop,
                        viewport.querySelector('[data-unread-boundary="101"]')
                            .offsetTop - 16,
                        "a long unread tail still opens at its first unread message"
                    );
                }
            }
        }
    );

    QUnit.test(
        "checks connection health through the public API and preserves cache on error",
        async (assert) => {
            const calls = [];
            const notifications = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: {
                    add(message, options) {
                        notifications.push({message, options});
                    },
                },
            });
            store.applyConnectionHealth({
                items: [
                    {
                        id: 10,
                        account_name: "Comercial 01",
                        state: "disconnected",
                        can_check: true,
                    },
                ],
            });
            store.state.bootstrap = {
                capabilities: {check_connection_health: true},
            };
            assert.strictEqual(store.canCheckConnections, true);
            store.call = async (method, args) => {
                calls.push({method, args});
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: [
                        {
                            id: 10,
                            account_name: "Comercial 01",
                            state: "disconnected",
                            checking: true,
                            can_check: true,
                        },
                    ],
                };
            };

            assert.strictEqual(await store.checkConnectionHealth(10), true);
            assert.deepEqual(calls[0], {
                method: "check_connection_health",
                args: [10],
            });
            assert.strictEqual(
                store.connectionHealth.items[0].state,
                "disconnected",
                "checking never erases the last-known health state"
            );
            assert.strictEqual(store.connectionHealth.items[0].checking, true);
            assert.strictEqual(store.state.connectionHealthCheckingId, false);
            assert.strictEqual(notifications[0].options.type, "success");

            const cached = store.connectionHealth;
            store.call = async () => {
                throw new Error("provider unavailable");
            };
            assert.strictEqual(await store.checkConnectionHealth(false), false);
            assert.strictEqual(
                store.connectionHealth,
                cached,
                "failure restores cache"
            );
            assert.strictEqual(
                store.state.connectionHealthError,
                "provider unavailable"
            );
            assert.strictEqual(
                await store.checkConnectionHealth("10"),
                false,
                "connection IDs are never coerced"
            );
            store.stopConnectionHealthRefresh();
        }
    );

    QUnit.test(
        "polls with bounded backoff until a lost health bus result converges",
        async (assert) => {
            const tasks = [];
            const delays = [];
            const healthTimer = {
                setTimeout(callback, delay) {
                    const task = {callback, cancelled: false};
                    tasks.push(task);
                    delays.push(delay);
                    return task;
                },
                clearTimeout(task) {
                    task.cancelled = true;
                    const index = tasks.indexOf(task);
                    if (index >= 0) {
                        tasks.splice(index, 1);
                    }
                },
            };
            const calls = [];
            let bootstrapCalls = 0;
            const healthSnapshot = (checking) => ({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                connection_health: {
                    items: [
                        {
                            id: 10,
                            account_name: "Comercial 01",
                            state: "connected",
                            checking,
                            can_check: true,
                        },
                    ],
                },
            });
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                healthTimer,
            });
            store.applyConnectionHealth(healthSnapshot(false).connection_health);
            store.call = async (method) => {
                calls.push(method);
                if (method === "check_connection_health") {
                    return healthSnapshot(true);
                }
                bootstrapCalls += 1;
                return healthSnapshot(bootstrapCalls === 1);
            };

            assert.strictEqual(await store.checkConnectionHealth(false), true);
            assert.strictEqual(store.connectionHealth.summary.checking, 1);
            assert.deepEqual(delays, [500], "the first fallback is prompt");

            await tasks.shift().callback();
            assert.strictEqual(
                store.connectionHealth.summary.checking,
                1,
                "a still-running snapshot keeps convergence active"
            );
            assert.deepEqual(delays, [500, 1000], "subsequent polls back off");

            await tasks.shift().callback();
            assert.strictEqual(store.connectionHealth.summary.checking, 0);
            assert.strictEqual(tasks.length, 0, "settlement cancels further polling");
            assert.deepEqual(calls, [
                "check_connection_health",
                "bootstrap",
                "bootstrap",
            ]);
            store.stopConnectionHealthRefresh();

            tasks.length = 0;
            delays.length = 0;
            const stuckStore = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                healthTimer,
            });
            stuckStore.call = async () => healthSnapshot(true);
            stuckStore.applyConnectionHealth(healthSnapshot(true).connection_health);
            while (tasks.length) {
                await tasks.shift().callback();
            }
            assert.strictEqual(delays.length, 11, "polling has a hard attempt cap");
            assert.strictEqual(
                delays.reduce((total, delay) => total + delay, 0),
                180500,
                "the bounded window covers a twenty-account queue"
            );
            assert.strictEqual(tasks.length, 0, "the capped poller stops cleanly");
            stuckStore.stopConnectionHealthRefresh();
        }
    );

    QUnit.test(
        "keeps a newer health bus result over a stale check response",
        async (assert) => {
            const tasks = [];
            const delays = [];
            const healthTimer = {
                setTimeout(callback, delay) {
                    const task = {callback};
                    tasks.push(task);
                    delays.push(delay);
                    return task;
                },
                clearTimeout(task) {
                    const index = tasks.indexOf(task);
                    if (index >= 0) {
                        tasks.splice(index, 1);
                    }
                },
            };
            const healthSnapshot = (checking) => ({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                connection_health: {
                    items: [
                        {
                            id: 10,
                            account_name: "Comercial 01",
                            state: "connected",
                            checking,
                            can_check: true,
                        },
                    ],
                },
            });
            let resolveCheck = null;
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                healthTimer,
            });
            store.applyConnectionHealth(healthSnapshot(false).connection_health);
            store.call = async (method) => {
                if (method === "check_connection_health") {
                    return new Promise((resolve) => {
                        resolveCheck = resolve;
                    });
                }
                return healthSnapshot(false);
            };

            const request = store.checkConnectionHealth(false);
            const healthEvent = (checking, lastCheckAt = "") => ({
                type: CONTACT_CENTER_NOTIFICATION_TYPE,
                payload: {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    event_type: CONNECTION_HEALTH_EVENT_TYPE,
                    connection_id: 10,
                    item: {
                        id: 10,
                        state: "connected",
                        checking,
                        last_check_at: lastCheckAt,
                    },
                },
            });
            store.onNotification({
                detail: [healthEvent(true), healthEvent(false, "2026-08-23 22:04:24")],
            });
            resolveCheck(healthSnapshot(true));

            assert.strictEqual(await request, true);
            assert.strictEqual(
                store.connectionHealth.summary.checking,
                0,
                "the older RPC snapshot cannot regress the completed bus state"
            );
            assert.deepEqual(
                delays,
                [500, 160],
                "the final bus event cancels polling before one confirmation"
            );

            let resolveRefresh = null;
            store.call = async () =>
                new Promise((resolve) => {
                    resolveRefresh = resolve;
                });
            const confirmation = tasks.shift().callback();
            store.onNotification({
                detail: [healthEvent(false, "2026-08-23 22:04:25")],
            });
            resolveRefresh(healthSnapshot(true));
            await confirmation;
            assert.strictEqual(store.connectionHealth.summary.checking, 0);
            assert.strictEqual(
                tasks.length,
                0,
                "an in-flight confirmation also preserves the newer bus event"
            );
            store.stopConnectionHealthRefresh();
        }
    );

    QUnit.test(
        "applies incremental health events and debounces snapshot-only invalidations",
        (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.applyConnectionHealth({
                items: [
                    {id: 10, account_name: "Comercial 01", state: "connected"},
                    {
                        id: 20,
                        account_name: "Comercial 02",
                        display_address: "+551199990020",
                        state: "connected",
                    },
                ],
            });
            let refreshes = 0;
            store.scheduleConnectionHealthRefresh = () => {
                refreshes += 1;
            };
            const event = (payload) => ({
                type: CONTACT_CENTER_NOTIFICATION_TYPE,
                payload: {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    event_type: CONNECTION_HEALTH_EVENT_TYPE,
                    connection_id: 20,
                    ...payload,
                },
            });

            store.onNotification({
                detail: [
                    event({
                        item: {
                            id: 20,
                            state: "authentication_required",
                        },
                    }),
                ],
            });
            assert.strictEqual(store.connectionHealth.summary.connected, 1);
            assert.strictEqual(
                store.connectionHealth.summary.authentication_required,
                1
            );
            assert.strictEqual(
                store.connectionHealth.items[0].account_name,
                "Comercial 02",
                "partial events preserve public identity fields"
            );
            assert.strictEqual(
                store.connectionHealth.items[0].display_address,
                "+551199990020",
                "partial events preserve the safe display address"
            );
            assert.strictEqual(refreshes, 0, "complete item updates locally");

            store.onNotification({detail: [event({})]});
            assert.strictEqual(refreshes, 1, "bare invalidation requests one refresh");

            store.onNotification({
                detail: [
                    {
                        type: CONTACT_CENTER_NOTIFICATION_TYPE,
                        payload: {
                            schema_version: SUPPORTED_SCHEMA_VERSION,
                            event_type: CONNECTION_HEALTH_EVENT_TYPE,
                            connection_id: 99,
                            item: {
                                id: 99,
                                account_name: "Fora do snapshot",
                                state: "disconnected",
                            },
                        },
                    },
                ],
            });
            assert.strictEqual(
                refreshes,
                2,
                "an unknown ID requests a scoped bootstrap refresh"
            );
            assert.strictEqual(
                store.connectionHealth.summary.total,
                2,
                "an unknown bus item is not inserted into the current fleet"
            );

            store.toggleConnectionHealth();
            assert.strictEqual(store.state.connectionHealthOpen, true);
            store.closeConnectionHealth();
            assert.strictEqual(store.state.connectionHealthOpen, false);
        }
    );

    QUnit.test(
        "an in-flight send never leaks into another conversation",
        async (assert) => {
            let resolveSend = null;
            let sentRequestId = "";
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.conversations = [
                {channel_id: 10, conversation_type: "direct", can_send: true},
                {channel_id: 20, conversation_type: "direct", can_send: true},
            ];
            store.state.selectedChannelId = 10;
            store.state.messages = [{message_id: 1, body_text: "Conversation A"}];
            store.call = (_method, args) =>
                new Promise((resolve) => {
                    sentRequestId = args[3];
                    resolveSend = resolve;
                });
            store.refreshLoadedConversations = async () => true;

            const pending = store.sendMessage("Outbound A");
            store.state.selectedChannelId = 20;
            store.state.messages = [{message_id: 2, body_text: "Conversation B"}];
            resolveSend({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 10,
                client_request_id: sentRequestId,
                message: {message_id: 3, body_text: "Outbound A"},
            });

            assert.strictEqual(await pending, true);
            assert.deepEqual(
                store.state.messages.map((message) => message.message_id),
                [2],
                "the response for A does not mutate B's timeline"
            );
        }
    );

    QUnit.test("sends independently in two conversations", async (assert) => {
        const pending = new Map();
        const store = new ContactCenterStore({
            orm: {},
            busService: {},
            notification: false,
        });
        store.state.conversations = [
            {channel_id: 10, conversation_type: "direct", can_send: true},
            {channel_id: 20, conversation_type: "direct", can_send: true},
        ];
        store.call = (method, args) =>
            new Promise((resolve) => pending.set(args[0], {method, args, resolve}));
        store.refreshLoadedConversations = async () => true;

        store.state.selectedChannelId = 10;
        const sendA = store.sendMessage("Mensagem A");
        assert.ok(store.isSending(10));
        assert.notOk(
            await store.sendMessage("Clique duplicado"),
            "the same conversation remains single-flight"
        );

        store.state.selectedChannelId = 20;
        const sendB = store.sendMessage("Mensagem B");
        assert.ok(store.isSending(10));
        assert.ok(store.isSending(20), "another conversation is not globally blocked");
        assert.notStrictEqual(
            pending.get(10).args[3],
            pending.get(20).args[3],
            "each conversation owns its idempotency key"
        );

        pending.get(10).resolve({
            schema_version: SUPPORTED_SCHEMA_VERSION,
            channel_id: 10,
            client_request_id: pending.get(10).args[3],
            message: {message_id: 101},
        });
        assert.ok(await sendA);
        assert.notOk(store.isSending(10));
        assert.ok(store.isSending(20), "finishing A cannot release B");

        pending.get(20).resolve({
            schema_version: SUPPORTED_SCHEMA_VERSION,
            channel_id: 20,
            client_request_id: pending.get(20).args[3],
            message: {message_id: 201},
        });
        assert.ok(await sendB);
        assert.notOk(store.isSending(20));
    });

    QUnit.test(
        "a conversation selection guard can preserve active work",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.messages = [{message_id: 1}];
            const unregister = store.registerConversationSelectionGuard(
                (channelId) => channelId !== 20
            );

            assert.notOk(await store.selectConversation(20));
            assert.strictEqual(store.state.selectedChannelId, 10);
            unregister();
            store.loadTimeline = async () => true;
            assert.ok(await store.selectConversation(20));
            assert.strictEqual(store.state.selectedChannelId, 20);
        }
    );

    QUnit.test(
        "composer preserves text, reply and uploaded media by conversation",
        (assert) => {
            const composer = Object.create(MessageComposer.prototype);
            composer.props = {
                state: {
                    replyTo: {message_id: 7, author: {name: "Cliente"}},
                },
            };
            composer.activeChannelId = 10;
            composer.drafts = new Map();
            composer.local = {
                body: "Resposta ainda não enviada",
                mode: "message",
                attachments: [
                    {
                        id: "upload-10",
                        name: "manual.pdf",
                        previewUrl: "blob:manual",
                        mediaRef: "media-ref-ready",
                        phase: "ready",
                        file: null,
                    },
                ],
                uploadError: "",
                scheduledAt: "2030-01-02T10:00",
                followupSummary: "Retornar proposta",
                followupNote: "Revisar escopo",
                followupDate: "2030-01-03",
                followupActivityTypeId: 4,
                followupUserId: 7,
            };
            composer.resize = () => true;

            assert.ok(composer.saveCurrentDraft());
            composer.local.body = "";
            composer.local.attachments = [];
            composer.local.scheduledAt = "";
            composer.local.followupSummary = "";
            composer.props.state.replyTo = false;

            assert.ok(composer.restoreDraft(10));
            assert.strictEqual(composer.local.body, "Resposta ainda não enviada");
            assert.strictEqual(composer.attachments[0].mediaRef, "media-ref-ready");
            assert.strictEqual(composer.attachments[0].name, "manual.pdf");
            assert.strictEqual(composer.props.state.replyTo.message_id, 7);
            assert.strictEqual(composer.local.scheduledAt, "2030-01-02T10:00");
            assert.strictEqual(composer.local.followupSummary, "Retornar proposta");
            assert.notOk(composer.drafts.has(10), "the active draft has one owner");
        }
    );

    QUnit.test(
        "a prepared upload releases the browser file and keeps its server reference",
        (assert) => {
            const composer = Object.create(MessageComposer.prototype);
            const item = {
                id: "upload-large-video",
                file: {name: "video-local.mp4", size: 50000000},
                metadata: {durationSeconds: 15},
                name: "video-local.mp4",
                size: 50000000,
                mimetype: "video/mp4",
                kind: "video",
                previewUrl: "blob:video-local",
                isVoiceNote: false,
                durationSeconds: 0,
                phase: "uploading",
            };
            let revoked = 0;
            composer.revokePreview = () => {
                revoked += 1;
            };

            composer.applyUploadedMedia(item, {
                media_ref: " prepared-media-ref ",
                media: {
                    kind: "video",
                    state: "ready",
                    name: "video.mp4",
                    mimetype: "video/mp4",
                    size_bytes: 50000000,
                    duration_seconds: 15,
                },
            });

            assert.strictEqual(item.mediaRef, "prepared-media-ref");
            assert.strictEqual(item.phase, "ready");
            assert.strictEqual(item.name, "video.mp4");
            assert.strictEqual(item.previewUrl, "");
            assert.strictEqual(revoked, 1);
            assert.strictEqual(item.file, null);
            assert.strictEqual(item.id, "upload-large-video");
            assert.deepEqual(item.metadata, {});
        }
    );

    QUnit.test("a prepared small upload keeps its local preview", (assert) => {
        const composer = Object.create(MessageComposer.prototype);
        const item = {
            id: "upload-small-image",
            file: {name: "image-local.jpg", size: 2000000},
            metadata: {},
            name: "image-local.jpg",
            size: 2000000,
            mimetype: "image/jpeg",
            kind: "image",
            previewUrl: "blob:image-local",
            isVoiceNote: false,
            durationSeconds: 0,
            mediaRef: "",
            phase: "uploading",
        };
        composer.revokePreview = () => assert.step("unexpected revoke");

        composer.applyUploadedMedia(item, {
            media_ref: "prepared-image-ref",
            media: {
                kind: "image",
                state: "ready",
                name: "image.jpg",
                mimetype: "image/jpeg",
                size_bytes: 2000000,
            },
        });

        assert.strictEqual(item.previewUrl, "blob:image-local");
        assert.strictEqual(item.file, null);
        assert.verifySteps([]);
    });

    QUnit.test(
        "composer persists an indirect conversation switch before resetting",
        (assert) => {
            const composer = Object.create(MessageComposer.prototype);
            composer.activeChannelId = 10;
            composer.switchPrepared = false;
            composer.local = {
                attachments: [{id: "pending", phase: "uploading"}],
                uploadError: "",
            };
            let saved = 0;
            let savedUploadPhase = "";
            const restored = [];
            composer.saveCurrentDraft = () => {
                saved += 1;
                savedUploadPhase = composer.attachments[0].phase;
                return true;
            };
            composer.resetLocalForConversation = () => true;
            composer.restoreDraft = (channelId) => restored.push(channelId);

            composer.activateConversation(20);
            assert.strictEqual(saved, 1, "an indirect switch saves the active draft");
            assert.strictEqual(
                savedUploadPhase,
                "error",
                "an interrupted upload is restorable through the existing retry action"
            );
            assert.deepEqual(restored, [20]);

            composer.switchPrepared = true;
            composer.activateConversation(30);
            assert.strictEqual(
                saved,
                1,
                "a direct switch already prepared by the guard is not saved twice"
            );
        }
    );

    QUnit.test("renames a guest through the scoped local API", async (assert) => {
        const calls = [];
        const store = new ContactCenterStore({
            orm: {},
            busService: {},
            notification: false,
        });
        store.state.bootstrap = {
            schema_version: SUPPORTED_SCHEMA_VERSION,
            capabilities: {rename_guest: true},
        };
        store.state.conversations = [
            {
                channel_id: 10,
                state: "open",
                conversation_type: "direct",
                name: "Identificador",
                identity: {id: 7, name: "Identificador", persona_kind: "guest"},
            },
        ];
        store.state.selectedChannelId = 10;
        store.call = async (method, args) => {
            calls.push({method, args});
            return {
                schema_version: SUPPORTED_SCHEMA_VERSION,
                identity: {id: 7, name: "Maria Solar", persona_kind: "guest"},
            };
        };

        assert.ok(await store.renameGuest("  Maria Solar  "));
        assert.deepEqual(calls, [{method: "rename_guest", args: [10, "Maria Solar"]}]);
        assert.strictEqual(store.state.conversations[0].name, "Maria Solar");
        assert.strictEqual(store.state.conversations[0].identity.name, "Maria Solar");

        store.state.bootstrap.capabilities.rename_guest = false;
        assert.notOk(await store.renameGuest("Bloqueado"));
        assert.strictEqual(calls.length, 1, "revoked capability blocks the RPC");
    });

    QUnit.test("selects only OGG Opus or MP4 for browser recording", (assert) => {
        const recordingPolicy = {
            recording_mimetypes: ["audio/ogg;codecs=opus", "audio/mp4"],
            voice_note_mimetypes: ["audio/ogg;codecs=opus"],
            max_duration_seconds: 900,
        };
        function recorderWithSupport(supported) {
            class Recorder {}
            Recorder.isTypeSupported = (mimetype) => supported.includes(mimetype);
            return Recorder;
        }

        assert.strictEqual(
            selectVoiceRecordingFormat(
                recorderWithSupport([
                    "audio/ogg;codecs=opus",
                    "audio/mp4",
                    "audio/webm;codecs=opus",
                ]),
                recordingPolicy
            ).mimeType,
            "audio/ogg;codecs=opus",
            "native OGG Opus is preferred for a true WhatsApp voice note"
        );
        assert.strictEqual(
            selectVoiceRecordingFormat(
                recorderWithSupport(["audio/mp4", "audio/webm;codecs=opus"]),
                recordingPolicy
            ).mimeType,
            "audio/mp4",
            "Chromium/Safari use compatible regular MP4 audio"
        );
        const webmOnly = recorderWithSupport(["audio/webm;codecs=opus"]);
        assert.notOk(
            selectVoiceRecordingFormat(webmOnly, {
                ...recordingPolicy,
                recording_mimetypes: ["audio/webm;codecs=opus"],
            }),
            "WebM is not offered without provider compatibility evidence"
        );
        assert.notOk(
            voiceRecorderAvailable(
                {
                    isSecureContext: true,
                    MediaRecorder: webmOnly,
                    navigator: {
                        mediaDevices: {getUserMedia: () => Promise.resolve(false)},
                    },
                },
                recordingPolicy
            ),
            "an incompatible recorder falls back to file attachment"
        );
        assert.deepEqual(
            voiceRecordingDescriptor("audio/ogg;codecs=opus", recordingPolicy),
            {
                mimeType: "audio/ogg",
                extension: "ogg",
                isVoiceNote: true,
            }
        );
        assert.deepEqual(voiceRecordingDescriptor("audio/mp4", recordingPolicy), {
            mimeType: "audio/mp4",
            extension: "m4a",
            isVoiceNote: false,
        });
        assert.notOk(
            voiceRecordingDescriptor("audio/webm;codecs=opus", recordingPolicy)
        );
        assert.notOk(
            selectVoiceRecordingFormat(recorderWithSupport(["audio/mp4"]), {
                enabled: true,
            }),
            "generic audio attachment support does not imply recorder support"
        );
    });

    QUnit.test(
        "records MP4 audio, stops tracks and exposes a preview file",
        async (assert) => {
            let intervalCallback = null;
            let now = 1000;
            let trackStops = 0;
            const phases = [];
            const completed = [];
            const stream = {
                getTracks: () => [{stop: () => (trackStops += 1)}],
            };
            class FakeMediaRecorder {
                static isTypeSupported(mimetype) {
                    return mimetype === "audio/mp4";
                }

                constructor(_stream, options) {
                    this.mimeType = options.mimeType;
                    this.state = "inactive";
                }

                start() {
                    this.state = "recording";
                }

                stop() {
                    this.state = "inactive";
                    this.ondataavailable({
                        data: new window.Blob(["recorded-audio"], {
                            type: this.mimeType,
                        }),
                    });
                    this.onstop();
                }
            }
            const scope = {
                Blob: window.Blob,
                File: window.File,
                MediaRecorder: FakeMediaRecorder,
                isSecureContext: true,
                navigator: {
                    mediaDevices: {getUserMedia: async () => stream},
                },
                setInterval(callback) {
                    intervalCallback = callback;
                    return 7;
                },
                clearInterval() {
                    intervalCallback = null;
                },
            };
            const session = new VoiceRecorderSession({
                scope,
                now: () => now,
                onPhase: (phase) => phases.push(phase),
                onComplete: (recording) => completed.push(recording),
            });

            assert.ok(
                await session.start({
                    recording_mimetypes: ["audio/mp4"],
                    voice_note_mimetypes: [],
                    max_duration_seconds: 900,
                })
            );
            assert.strictEqual(session.phase, "recording");
            now = 3500;
            intervalCallback();
            assert.ok(session.stop());

            assert.deepEqual(phases.slice(-3), ["recording", "processing", "idle"]);
            assert.strictEqual(completed.length, 1);
            assert.strictEqual(completed[0].file.type, "audio/mp4");
            assert.ok(completed[0].file.name.endsWith(".m4a"));
            assert.strictEqual(completed[0].durationSeconds, 3);
            assert.notOk(completed[0].isVoiceNote);
            assert.strictEqual(
                trackStops,
                1,
                "the microphone track is always released"
            );
            assert.strictEqual(
                intervalCallback,
                null,
                "the recording timer is cleared"
            );
        }
    );

    QUnit.test(
        "cancels a pending microphone request without leaking its track",
        async (assert) => {
            let resolvePermission = null;
            let trackStops = 0;
            let completed = 0;
            class FakeMediaRecorder {
                static isTypeSupported(mimetype) {
                    return mimetype === "audio/mp4";
                }
            }
            const scope = {
                Blob: window.Blob,
                File: window.File,
                MediaRecorder: FakeMediaRecorder,
                isSecureContext: true,
                navigator: {
                    mediaDevices: {
                        getUserMedia: () =>
                            new Promise((resolve) => {
                                resolvePermission = resolve;
                            }),
                    },
                },
                setInterval: () => 1,
                clearInterval: () => true,
            };
            const session = new VoiceRecorderSession({
                scope,
                onComplete: () => (completed += 1),
            });
            const pending = session.start({
                recording_mimetypes: ["audio/mp4"],
                voice_note_mimetypes: [],
                max_duration_seconds: 900,
            });

            session.cancel();
            resolvePermission({
                getTracks: () => [{stop: () => (trackStops += 1)}],
            });

            assert.notOk(await pending);
            assert.strictEqual(session.phase, "idle");
            assert.strictEqual(trackStops, 1, "late permission tracks are stopped");
            assert.strictEqual(
                completed,
                0,
                "a cancelled capture cannot create a file"
            );
        }
    );

    QUnit.test(
        "releases the microphone when MediaRecorder never emits stop",
        async (assert) => {
            let timeoutCallback = null;
            let trackStops = 0;
            const errors = [];
            class StalledMediaRecorder {
                static isTypeSupported(mimetype) {
                    return mimetype === "audio/mp4";
                }

                constructor(_stream, options) {
                    this.mimeType = options.mimeType;
                    this.state = "inactive";
                }

                start() {
                    this.state = "recording";
                }

                stop() {
                    this.state = "inactive";
                }
            }
            const scope = {
                Blob: window.Blob,
                File: window.File,
                MediaRecorder: StalledMediaRecorder,
                isSecureContext: true,
                navigator: {
                    mediaDevices: {
                        getUserMedia: async () => ({
                            getTracks: () => [{stop: () => (trackStops += 1)}],
                        }),
                    },
                },
                setInterval: () => 1,
                clearInterval: () => true,
                setTimeout(callback) {
                    timeoutCallback = callback;
                    return 2;
                },
                clearTimeout() {
                    timeoutCallback = null;
                },
            };
            const session = new VoiceRecorderSession({
                scope,
                onError: (message) => errors.push(message),
            });

            assert.ok(
                await session.start({
                    recording_mimetypes: ["audio/mp4"],
                    voice_note_mimetypes: [],
                    max_duration_seconds: 120,
                })
            );
            assert.ok(session.stop());
            assert.strictEqual(session.phase, "processing");
            assert.strictEqual(trackStops, 0);

            timeoutCallback();
            assert.strictEqual(session.phase, "idle");
            assert.strictEqual(trackStops, 1);
            assert.strictEqual(errors.length, 1);
        }
    );

    QUnit.test(
        "ignores a late stop callback from the previous recording",
        async (assert) => {
            const recorders = [];
            const trackStops = [0, 0];
            let streamIndex = 0;
            let timerSequence = 0;
            class FakeMediaRecorder {
                static isTypeSupported(mimetype) {
                    return mimetype === "audio/mp4";
                }

                constructor(stream, options) {
                    this.stream = stream;
                    this.mimeType = options.mimeType;
                    this.state = "inactive";
                    recorders.push(this);
                }

                start() {
                    this.state = "recording";
                }

                stop() {
                    this.state = "inactive";
                }
            }
            const streams = trackStops.map((_value, index) => ({
                id: index,
                getTracks: () => [{stop: () => (trackStops[index] += 1)}],
            }));
            const scope = {
                Blob: window.Blob,
                File: window.File,
                MediaRecorder: FakeMediaRecorder,
                isSecureContext: true,
                navigator: {
                    mediaDevices: {
                        getUserMedia: async () => streams[streamIndex++],
                    },
                },
                setInterval: () => ++timerSequence,
                clearInterval: () => true,
            };
            const session = new VoiceRecorderSession({scope});

            const recordingPolicy = {
                recording_mimetypes: ["audio/mp4"],
                voice_note_mimetypes: [],
                max_duration_seconds: 900,
            };
            assert.ok(await session.start(recordingPolicy));
            const firstRecorder = recorders[0];
            session.cancel();
            assert.ok(await session.start(recordingPolicy));
            const secondRecorder = recorders[1];
            const secondTimer = session.timer;

            firstRecorder.onstop();

            assert.strictEqual(session.phase, "recording");
            assert.strictEqual(session.recorder, secondRecorder);
            assert.strictEqual(session.stream, streams[1]);
            assert.strictEqual(session.timer, secondTimer);
            assert.deepEqual(trackStops, [1, 0]);

            session.cancel();
        }
    );

    QUnit.test(
        "formats recorder feedback and the optional signature hint",
        (assert) => {
            assert.strictEqual(formatVoiceRecordingDuration(0), "00:00");
            assert.strictEqual(formatVoiceRecordingDuration(125.9), "02:05");
            assert.strictEqual(
                voiceRecordingErrorMessage({name: "NotAllowedError"}),
                "Permita o acesso ao microfone para gravar áudio."
            );
            assert.strictEqual(
                composerHint(
                    {
                        account: {signature_enabled: true},
                        capabilities: {sender_signature: true},
                    },
                    {user: {name: "Lucas Operador"}}
                ),
                "Assinada como Lucas Operador · Enter envia · Shift+Enter quebra linha"
            );
            assert.strictEqual(
                composerHint(
                    {account: {signature_enabled: false}},
                    {user: {name: "Lucas"}}
                ),
                "Enter envia · Shift+Enter quebra linha"
            );
            assert.strictEqual(
                composerHint(
                    {
                        account: {signature_enabled: true},
                        capabilities: {sender_signature: false},
                    },
                    {user: {name: "Lucas"}}
                ),
                "Enter envia · Shift+Enter quebra linha",
                "the UI does not promise a signature the provider cannot send"
            );
        }
    );

    QUnit.test("recording interlocks the normal composer send path", (assert) => {
        const composer = Object.create(MessageComposer.prototype);
        composer.props = {
            conversation: {
                channel_id: 10,
                conversation_type: "direct",
                can_send: true,
                capabilities: {media: {audio: {enabled: true}}},
            },
            state: {replyTo: false, sending: false},
        };
        composer.local = {
            body: "mensagem concorrente",
            attachments: [],
            recordingPhase: "idle",
        };

        assert.ok(composer.canSend, "text can be sent while the recorder is idle");
        composer.local.recordingPhase = "recording";
        assert.notOk(composer.canSend, "recording blocks text send until it resolves");
    });

    QUnit.test("uploads media as authenticated multipart form data", async (assert) => {
        const fields = [];
        const requests = [];
        const file = {name: "manual.pdf"};
        const formData = {
            append(...args) {
                fields.push(args);
            },
        };
        const signal = {aborted: false};
        const payload = await uploadMediaRequest({
            channelId: 10,
            file,
            clientUploadId: "upload-request-1",
            csrfToken: "csrf-value",
            signal,
            formDataFactory: () => formData,
            fetcher: async (url, options) => {
                requests.push({url, options});
                return {
                    ok: true,
                    json: async () => ({
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        media_ref: "upload-ref-1",
                        media: {id: 5},
                    }),
                };
            },
        });

        assert.strictEqual(payload.media_ref, "upload-ref-1");
        assert.deepEqual(fields, [
            ["channel_id", "10"],
            ["client_upload_id", "upload-request-1"],
            ["file", file, "manual.pdf"],
            ["csrf_token", "csrf-value"],
        ]);
        assert.strictEqual(requests[0].url, "/contact_center/media/upload");
        assert.strictEqual(requests[0].options.method, "POST");
        assert.strictEqual(requests[0].options.credentials, "same-origin");
        assert.strictEqual(requests[0].options.body, formData);
        assert.strictEqual(requests[0].options.signal, signal);
    });

    QUnit.test("maps an upload authorization failure to pt-BR", async (assert) => {
        await assert.rejects(
            uploadMediaRequest({
                channelId: 10,
                file: {name: "arquivo.pdf"},
                clientUploadId: "upload-forbidden",
                formDataFactory: () => ({append: () => true}),
                fetcher: async () => ({status: 403, ok: false}),
            }),
            /Sua sessão não permite enviar este arquivo/
        );
    });

    QUnit.test(
        "uploads recorded audio with duration and PTT intent",
        async (assert) => {
            const fields = [];
            const file = {name: "recording.ogg"};
            await uploadMediaRequest({
                channelId: 10,
                file,
                clientUploadId: "recorded-audio-request",
                isVoiceNote: true,
                durationSeconds: 12,
                csrfToken: "csrf-value",
                formDataFactory: () => ({append: (...args) => fields.push(args)}),
                fetcher: async () => ({
                    ok: true,
                    json: async () => ({
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        media_ref: "recorded-audio-ref",
                        media: {
                            kind: "audio",
                            is_voice_note: true,
                            duration_seconds: 12,
                        },
                    }),
                }),
            });

            assert.deepEqual(fields, [
                ["channel_id", "10"],
                ["client_upload_id", "recorded-audio-request"],
                ["file", file, "recording.ogg"],
                ["duration_seconds", "12"],
                ["is_voice_note", "1"],
                ["csrf_token", "csrf-value"],
            ]);

            const regularFields = [];
            await uploadMediaRequest({
                channelId: 10,
                file: {name: "recording.m4a"},
                clientUploadId: "regular-audio-request",
                durationSeconds: 8,
                csrfToken: "",
                formDataFactory: () => ({
                    append: (...args) => regularFields.push(args),
                }),
                fetcher: async () => ({
                    ok: true,
                    json: async () => ({
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        media_ref: "regular-audio-ref",
                        media: {kind: "audio", duration_seconds: 8},
                    }),
                }),
            });
            assert.deepEqual(regularFields.slice(-1), [["duration_seconds", "8"]]);
            assert.notOk(
                regularFields.some(([name]) => name === "is_voice_note"),
                "MP4 recording remains regular audio rather than an invalid PTT"
            );
        }
    );

    QUnit.test(
        "uploads and sends one attachment without requiring text",
        async (assert) => {
            const uploads = [];
            const calls = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                uploadRequest: async (values) => {
                    uploads.push(values);
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        media_ref: "upload-ref-1",
                        media: {id: 5, kind: "document", state: "ready"},
                    };
                },
            });
            store.state.conversations = [
                {
                    channel_id: 10,
                    conversation_type: "direct",
                    can_send: true,
                    capabilities: {media: {document: {enabled: true}}},
                },
            ];
            store.state.selectedChannelId = 10;
            store.call = async (method, args) => {
                calls.push({method, args});
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 10,
                    client_request_id: args[3],
                    message: {message_id: 90, body_text: "", media: []},
                };
            };
            store.refreshLoadedConversations = async () => true;
            const file = {name: "manual.pdf", type: "application/pdf", size: 1200};

            const uploaded = await store.uploadMedia(file, "upload-request-1", 10);
            assert.strictEqual(uploaded.media_ref, "upload-ref-1");
            assert.deepEqual(uploads[0], {
                channelId: 10,
                file,
                clientUploadId: "upload-request-1",
                signal: undefined,
            });
            assert.strictEqual(await store.sendMessage("", [uploaded.media_ref]), true);
            assert.strictEqual(calls[0].method, "send_message");
            assert.deepEqual(calls[0].args.slice(0, 3), [10, "", false]);
            assert.deepEqual(calls[0].args[4], ["upload-ref-1"]);
            assert.ok(
                /^[0-9a-f-]{36}$/.test(calls[0].args[3]),
                "the attachment send keeps the composer request UUID"
            );
        }
    );

    QUnit.test("uses explicit message action RPC contracts", async (assert) => {
        const calls = [];
        const store = new ContactCenterStore({
            orm: {},
            busService: {},
            notification: false,
        });
        store.state.conversations = [
            {channel_id: 10, conversation_type: "direct", can_send: true},
        ];
        store.state.selectedChannelId = 10;
        store.state.messages = [
            {
                message_id: 7,
                body_text: "Antes",
                actions: {reply: true, react: true, edit: true, delete: true},
            },
        ];
        store.call = async (method, args) => {
            calls.push({method, args});
            return {
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: args[0],
                client_request_id: args[args.length - 1],
                message: {message_id: 7, body_text: "Depois"},
            };
        };
        store.refreshLoadedConversations = async () => true;

        assert.ok(await store.reactMessage(7, "👍", "add"));
        assert.ok(await store.editMessage(7, "Depois"));
        assert.ok(await store.deleteMessage(7));
        assert.deepEqual(
            calls.map(({method}) => method),
            ["react_message", "edit_message", "delete_message"]
        );
        assert.deepEqual(calls[0].args.slice(0, 4), [10, 7, "👍", "add"]);
        assert.deepEqual(calls[1].args.slice(0, 3), [10, 7, "Depois"]);
        assert.deepEqual(calls[2].args.slice(0, 2), [10, 7]);
        assert.ok(
            calls.every(({args}) => /^[0-9a-f-]{36}$/.test(args[args.length - 1]))
        );
    });

    QUnit.test(
        "resends through one idempotent action and merges source plus new attempt",
        async (assert) => {
            const calls = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.conversations = [
                {channel_id: 10, conversation_type: "direct", can_send: true},
            ];
            store.state.selectedChannelId = 10;
            store.state.messages = [
                {
                    message_id: 7,
                    body_text: "Original",
                    dispatch_reason: "permanent_failure",
                    actions: {resend: true},
                },
            ];
            store.call = async (method, args) => {
                calls.push({method, args});
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: args[0],
                    client_request_id: args[args.length - 1],
                    source_message: {
                        message_id: 7,
                        body_text: "Original",
                        dispatch_reason: "retry_created",
                        actions: {resend: false},
                    },
                    message: {
                        message_id: 8,
                        body_text: "Original",
                        dispatch_reason: "waiting_queue",
                        retry_of_message_id: 7,
                        actions: {resend: false},
                    },
                };
            };
            store.refreshLoadedConversations = async () => true;

            assert.ok(await store.resendMessage(7));
            assert.strictEqual(calls.length, 1);
            assert.strictEqual(calls[0].method, "resend_message");
            assert.deepEqual(calls[0].args.slice(0, 2), [10, 7]);
            assert.ok(/^[0-9a-f-]{36}$/.test(calls[0].args[2]));
            assert.deepEqual(
                store.state.messages.map((message) => message.message_id),
                [7, 8]
            );
            assert.strictEqual(
                store.state.messages[0].dispatch_reason,
                "retry_created"
            );
            assert.strictEqual(store.state.messages[0].actions.resend, false);
            assert.strictEqual(store.state.messages[1].retry_of_message_id, 7);

            store.state.messages[0] = {
                ...store.state.messages[0],
                actions: {...store.state.messages[0].actions, resend: true},
            };
            store.state.conversations[0] = {
                ...store.state.conversations[0],
                can_send: false,
            };
            assert.notOk(await store.resendMessage(7));
            assert.strictEqual(calls.length, 1, "revoked send policy stops the RPC");
        }
    );

    QUnit.test(
        "keeps a resend double-click single-flight in the timeline",
        async (assert) => {
            let release = () => undefined;
            let calls = 0;
            const timeline = {
                ui: {busyId: false, openMenuId: 7, reactionPickerId: false},
                store: {
                    resendMessage: async () => {
                        calls += 1;
                        return new Promise((resolve) => {
                            release = resolve;
                        });
                    },
                },
                isBusy: ConversationTimeline.prototype.isBusy,
                messageActionAllowed: () => true,
                closeActions: ConversationTimeline.prototype.closeActions,
            };
            const message = {message_id: 7};

            const first = ConversationTimeline.prototype.resend.call(timeline, message);
            assert.strictEqual(timeline.ui.busyId, 7);
            assert.notOk(
                await ConversationTimeline.prototype.resend.call(timeline, message),
                "a second click does not issue another RPC"
            );
            assert.strictEqual(calls, 1);
            release(true);
            assert.ok(await first);
            assert.strictEqual(timeline.ui.busyId, false);
        }
    );

    QUnit.test(
        "routes enabled group actions through provider-neutral RPC contracts",
        async (assert) => {
            const calls = [];
            const uploads = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                uploadRequest: async (values) => {
                    uploads.push(values);
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        media_ref: "group-media",
                        media: {id: 12, kind: "image", state: "ready"},
                    };
                },
            });
            store.state.conversations = [
                {
                    channel_id: 10,
                    conversation_type: "group",
                    can_send: true,
                    capabilities: {
                        media: {image: {enabled: true}},
                        reply: true,
                        react: true,
                        edit_message: true,
                        delete_message: true,
                    },
                },
            ];
            store.state.selectedChannelId = 10;
            store.state.messages = [
                {
                    message_id: 7,
                    body_text: "Alvo",
                    actions: {reply: true, react: true, edit: true, delete: true},
                    protocol_participant: {
                        jid: "5511999999999@s.whatsapp.net",
                        lid: "123456789@lid",
                    },
                },
            ];
            store.call = async (method, args) => {
                calls.push({method, args});
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: args[0],
                    client_request_id:
                        method === "send_message" ? args[3] : args[args.length - 1],
                    message:
                        method === "send_message"
                            ? {
                                  message_id: 8,
                                  body_text: "Legenda",
                                  actions: {},
                              }
                            : {
                                  message_id: 7,
                                  actions: {
                                      reply: true,
                                      react: true,
                                      edit: true,
                                      delete: true,
                                  },
                              },
                };
            };
            store.refreshLoadedConversations = async () => true;

            store.setReply(store.state.messages[0]);
            assert.strictEqual(store.state.replyTo.message_id, 7);
            const uploaded = await store.uploadMedia(
                {name: "foto.jpg", type: "image/jpeg", size: 100},
                "group-upload",
                10
            );
            assert.strictEqual(uploaded.media_ref, "group-media");
            assert.ok(await store.sendMessage("Legenda", [uploaded.media_ref]));
            assert.ok(await store.reactMessage(7, "👍", "add"));
            assert.ok(await store.editMessage(7, "Alteração"));
            assert.ok(await store.deleteMessage(7));
            assert.deepEqual(
                calls.map(({method}) => method),
                ["send_message", "react_message", "edit_message", "delete_message"],
                "all operations stay on the provider-neutral local API"
            );
            assert.deepEqual(calls[0].args.slice(0, 3), [10, "Legenda", 7]);
            assert.deepEqual(calls[0].args[4], ["group-media"]);
            assert.deepEqual(calls[1].args.slice(0, 4), [10, 7, "👍", "add"]);
            assert.deepEqual(calls[2].args.slice(0, 3), [10, 7, "Alteração"]);
            assert.deepEqual(calls[3].args.slice(0, 2), [10, 7]);
            assert.ok(
                calls
                    .slice(1)
                    .every(({args}) => /^[0-9a-f-]{36}$/.test(args[args.length - 1])),
                "every group mutation keeps its idempotency UUID"
            );
            assert.notOk(
                JSON.stringify(calls).includes("whatsapp.net") ||
                    JSON.stringify(calls).includes("@lid"),
                "protocol identifiers never cross the UI action contract"
            );
            const actionCallCount = calls.length;
            store.state.conversations[0] = {
                ...store.state.conversations[0],
                can_send: false,
            };
            assert.notOk(await store.reactMessage(7, "👍", "remove"));
            store.state.conversations[0] = {
                ...store.state.conversations[0],
                can_send: true,
            };
            store.state.messages[0] = {
                ...store.state.messages[0],
                actions: {...store.state.messages[0].actions, react: false},
            };
            assert.notOk(await store.reactMessage(7, "👍", "remove"));
            assert.strictEqual(
                calls.length,
                actionCallCount,
                "revoked policy and stale message actions are blocked before RPC"
            );
            assert.strictEqual(uploads.length, 1);
        }
    );

    QUnit.test(
        "applies a mutation result incrementally without discarding history",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.conversations = [
                {
                    channel_id: 10,
                    conversation_type: "group",
                    can_send: true,
                    capabilities: {react: true},
                },
            ];
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.messages = Array.from({length: 150}, (_item, index) => ({
                message_id: index + 1,
                body_text: `Mensagem ${index + 1}`,
                actions: {
                    reply: false,
                    react: index === 149,
                    edit: false,
                    delete: false,
                },
            }));
            store.state.timelineHasMore = true;
            store.state.nextBeforeMessageId = 1;
            store.call = async (_method, args) => ({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: args[0],
                client_request_id: args[args.length - 1],
                message: {
                    message_id: 150,
                    reactions: [{emoji: "👍", count: 1, reacted_by_me: true}],
                    actions: {react: true},
                },
            });
            store.refreshLoadedConversations = async () => true;
            let latestPageRefreshes = 0;
            store.refreshLatestTimeline = async () => {
                latestPageRefreshes += 1;
                return true;
            };
            store.loadTimeline = async () => {
                throw new Error("a mutation must not reset the timeline");
            };

            assert.ok(await store.reactMessage(150, "👍", "add"));
            assert.strictEqual(
                latestPageRefreshes,
                0,
                "the canonical mutation UiDTO is applied without a redundant RPC"
            );
            assert.strictEqual(store.state.messages.length, 150);
            assert.strictEqual(
                store.state.messages[149].body_text,
                "Mensagem 150",
                "partial mutation data preserves the already loaded body"
            );
            assert.deepEqual(store.state.messages[149].reactions, [
                {emoji: "👍", count: 1, reacted_by_me: true},
            ]);
            assert.strictEqual(store.state.nextBeforeMessageId, 1);
            assert.ok(store.state.timelineHasMore);
        }
    );

    QUnit.test(
        "reconciles a selected reply when conversation capabilities change",
        (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            const group = {
                channel_id: 10,
                state: "open",
                conversation_type: "group",
                can_send: true,
                capabilities: {media: {image: {enabled: true}}, reply: true},
            };
            const target = {
                message_id: 7,
                body_text: "Alvo",
                actions: {reply: true, react: false, edit: false, delete: false},
            };
            store.state.conversations = [group];
            store.state.selectedChannelId = 10;
            store.state.messages = [target];
            store.setReply(target);

            assert.strictEqual(store.state.replyTo.message_id, 7);
            assert.ok(
                store.replaceConversation({
                    ...group,
                    capabilities: {media: {}, reply: false},
                })
            );
            assert.notOk(store.state.replyTo, "a revoked reply is removed immediately");
            assert.notOk(
                store.prepareSendContext(store.selectedConversation, "", [
                    "stale-media",
                ]),
                "stale media cannot cross a revoked attachment capability"
            );

            const direct = {
                channel_id: 20,
                state: "open",
                conversation_type: "direct",
                can_send: true,
                capabilities: {media: {}},
            };
            store.state.conversations = [direct];
            store.state.selectedChannelId = 20;
            store.state.messages = [target];
            store.setReply(target);
            store.replaceConversation({...direct, name: "Atualizada"});
            assert.strictEqual(
                store.state.replyTo.message_id,
                7,
                "direct reply remains available after a normal conversation refresh"
            );
        }
    );

    QUnit.test(
        "selected refresh discovers a tag without reloading bootstrap",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            const existingTag = {id: 1, name: "Comercial", color: 2};
            const discoveredTag = {id: 7, name: "Prioridade", color: 5};
            store.state.bootstrap = {tags: [existingTag]};
            store.state.conversations = [openConversation({channel_id: 60, tags: []})];
            store.state.selectedChannelId = 60;
            let bootstrapLoads = 0;
            store.loadBootstrap = async () => {
                bootstrapLoads += 1;
                return true;
            };
            store.call = async (method, args) => {
                assert.strictEqual(method, "get_conversation");
                assert.deepEqual(args, [60]);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    item: openConversation({
                        channel_id: 60,
                        tags: [discoveredTag],
                    }),
                };
            };

            assert.ok(await store.refreshSelectedConversation({silent: true}));
            assert.strictEqual(bootstrapLoads, 0, "the full bootstrap stays untouched");
            assert.deepEqual(store.tags, [existingTag, discoveredTag]);
            assert.deepEqual(store.selectedConversation.tags, [discoveredTag]);
        }
    );

    QUnit.test(
        "selected refresh deduplicates tags and lets conversation values win",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {
                tags: [
                    {id: 3, name: "Mantida", color: 3},
                    {id: 1, name: "Versão antiga", color: 1},
                    {id: 1, name: "Duplicata antiga", color: 8},
                ],
            };
            store.state.conversations = [openConversation({channel_id: 60, tags: []})];
            store.state.selectedChannelId = 60;
            const refreshedTags = [
                {id: 1, name: "Versão atual", color: 4},
                {id: 8, name: "Zulu", color: 8},
                {id: 7, name: "Alpha", color: 7},
                {id: 7, name: "Alpha atual", color: 9},
            ];
            store.call = async () => ({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                item: openConversation({channel_id: 60, tags: refreshedTags}),
            });

            assert.ok(await store.refreshSelectedConversation({silent: true}));
            assert.deepEqual(
                store.tags,
                [
                    {id: 3, name: "Mantida", color: 3},
                    {id: 1, name: "Versão atual", color: 4},
                    {id: 7, name: "Alpha atual", color: 9},
                    {id: 8, name: "Zulu", color: 8},
                ],
                "existing positions remain stable, new ids are sorted, and fresh values win"
            );
            assert.strictEqual(
                store.tags.filter((tag) => tag.id === 1).length,
                1,
                "a stale bootstrap duplicate is removed"
            );

            const stableCatalog = store.tags;
            assert.ok(await store.refreshSelectedConversation({silent: true}));
            assert.strictEqual(
                store.tags,
                stableCatalog,
                "an identical refresh does not reorder or replace the catalog"
            );
        }
    );

    QUnit.test(
        "a discovered tag survives conversation selection and window changes",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            const discoveredTag = {id: 12, name: "Retorno", color: 6};
            store.state.bootstrap = {tags: []};
            store.state.conversations = [openConversation({channel_id: 60, tags: []})];
            store.state.selectedChannelId = 60;
            store.call = async () => ({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                item: openConversation({channel_id: 60, tags: [discoveredTag]}),
            });
            assert.ok(await store.refreshSelectedConversation({silent: true}));

            store.state.selectedChannelId = 61;
            store.applyConversationPage(
                {
                    items: [openConversation({channel_id: 61, tags: []})],
                    has_more: false,
                    next_cursor: false,
                    total: 1,
                },
                {reset: true, silent: false, previousConversation: false}
            );
            store.call = async () => ({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                item: openConversation({channel_id: 61, tags: []}),
            });
            assert.ok(await store.refreshSelectedConversation({silent: true}));
            assert.deepEqual(
                store.tags,
                [discoveredTag],
                "the catalog is independent from the currently loaded conversation window"
            );
        }
    );

    QUnit.test(
        "silent realtime sync preserves an off-page active conversation",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.listPhase = "ready";
            store.state.conversations = [
                {channel_id: 60, can_send: true, name: "Active page two"},
            ];
            store.state.selectedChannelId = 60;
            store.call = async () => ({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                items: [{channel_id: 1, name: "Top page"}],
                has_more: true,
                next_cursor: activityConversationCursor(1, "2026-08-21 12:00:00"),
                total: 60,
            });

            assert.strictEqual(
                await store.loadConversations({reset: true, silent: true}),
                true
            );
            assert.strictEqual(store.state.selectedChannelId, 60);
            assert.deepEqual(
                store.state.conversations.map((item) => item.channel_id),
                [1, 60]
            );

            store.call = async () => {
                throw new Error("temporary sync failure");
            };
            assert.strictEqual(
                await store.loadConversations({reset: true, silent: true}),
                false
            );
            assert.strictEqual(
                store.state.listPhase,
                "ready",
                "silent failures keep cached data visible"
            );

            const revoked = new Error("membership revoked");
            revoked.data = {name: "odoo.exceptions.AccessError"};
            store.call = async () => {
                throw revoked;
            };
            assert.strictEqual(
                await store.refreshSelectedConversation({silent: true}),
                false
            );
            assert.strictEqual(
                store.state.selectedChannelId,
                false,
                "an explicit access error clears the revoked conversation"
            );
        }
    );

    QUnit.test(
        "realtime refresh preserves every loaded conversation page and cursor",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.listPhase = "ready";
            store.state.conversations = Array.from({length: 120}, (_value, index) => ({
                channel_id: index + 1,
                name: `Cached ${index + 1}`,
            }));
            store.state.conversationsHaveMore = true;
            store.state.nextConversationCursor = activityConversationCursor(
                120,
                "2026-08-21 10:00:00"
            );
            store.state.selectedChannelId = 110;
            const calls = [];
            store.call = async (_method, _args, kwargs) => {
                calls.push(kwargs);
                if (calls.length === 1) {
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        items: Array.from({length: 100}, (_value, index) => ({
                            channel_id: index + 1,
                            name: `Fresh ${index + 1}`,
                        })),
                        has_more: true,
                        next_cursor: activityConversationCursor(
                            100,
                            "2026-08-21 11:00:00"
                        ),
                        total: 180,
                    };
                }
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: Array.from({length: 20}, (_value, index) => ({
                        channel_id: index + 101,
                        name: `Fresh ${index + 101}`,
                    })),
                    has_more: true,
                    next_cursor: activityConversationCursor(120, "2026-08-21 10:30:00"),
                    total: false,
                };
            };

            assert.strictEqual(
                await store.refreshLoadedConversations({silent: true}),
                true
            );
            assert.strictEqual(calls.length, 2);
            assert.strictEqual(calls[0].limit, 100);
            assert.strictEqual(calls[0].cursor, false);
            assert.strictEqual(calls[1].limit, 20);
            assert.deepEqual(
                calls[1].cursor,
                activityConversationCursor(100, "2026-08-21 11:00:00")
            );
            assert.strictEqual(store.state.conversations.length, 120);
            assert.strictEqual(store.state.conversations[119].channel_id, 120);
            assert.deepEqual(
                store.state.nextConversationCursor,
                activityConversationCursor(120, "2026-08-21 10:30:00")
            );
            assert.strictEqual(store.state.conversationTotal, 180);
            assert.ok(store.state.conversationsHaveMore);
            assert.strictEqual(
                store.state.selectedChannelId,
                110,
                "the normal <= 200 refresh keeps a still-visible selection"
            );
        }
    );

    QUnit.test(
        "realtime refresh does not grow its window from a preserved selected row",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            const selected = {channel_id: 999, name: "Selected outside window"};
            store.state.conversations = [
                ...Array.from({length: 49}, (_value, index) => ({
                    channel_id: index + 1,
                    name: `Cached ${index + 1}`,
                })),
                selected,
            ];
            store.state.selectedChannelId = selected.channel_id;
            const requestedLimits = [];
            store.call = async (_method, _args, kwargs) => {
                requestedLimits.push(kwargs.limit);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: Array.from({length: kwargs.limit}, (_value, index) => ({
                        channel_id: index + 1,
                        name: `Fresh ${index + 1}`,
                    })),
                    has_more: true,
                    next_cursor: activityConversationCursor(
                        kwargs.limit,
                        conversationActivityAt(kwargs.limit)
                    ),
                    total: 500,
                };
            };

            for (let iteration = 0; iteration < 3; iteration += 1) {
                assert.strictEqual(
                    await store.refreshLoadedConversations({silent: true}),
                    true
                );
            }

            assert.deepEqual(requestedLimits, [50, 50, 50]);
            assert.strictEqual(store.state.conversations.length, 51);
            assert.strictEqual(store.selectedConversation.channel_id, 999);
        }
    );

    QUnit.test(
        "realtime refresh preserves an oversized loaded window and its frontier",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.conversations = Array.from({length: 250}, (_value, index) => ({
                channel_id: index + 1,
                name: `Cached ${index + 1}`,
                last_activity_at: conversationActivityAt(index + 1),
            }));
            store.state.conversationsHaveMore = true;
            store.state.nextConversationCursor = activityConversationCursor(
                250,
                conversationActivityAt(250)
            );
            store.state.selectedChannelId = 225;
            const requestedLimits = [];
            store.call = async (_method, _args, kwargs) => {
                requestedLimits.push(kwargs.limit);
                const start = requestedLimits.length === 1 ? 0 : 100;
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: Array.from({length: kwargs.limit}, (_value, index) => ({
                        channel_id: start + index + 1,
                        name: `Fresh ${start + index + 1}`,
                        last_activity_at: conversationActivityAt(start + index + 1),
                    })),
                    has_more: true,
                    next_cursor: activityConversationCursor(
                        start + kwargs.limit,
                        conversationActivityAt(start + kwargs.limit)
                    ),
                    total: 500,
                };
            };

            assert.strictEqual(
                await store.refreshLoadedConversations({silent: true}),
                true
            );
            assert.deepEqual(requestedLimits, [100, 100]);
            assert.strictEqual(
                store.state.conversations.length,
                250,
                "a bounded refresh must not discard pages already loaded by the user"
            );
            assert.deepEqual(
                store.state.conversations.map((item) => item.channel_id),
                Array.from({length: 250}, (_value, index) => index + 1),
                "the refreshed prefix and cached tail retain deterministic order"
            );
            assert.strictEqual(store.state.conversations[199].name, "Fresh 200");
            assert.strictEqual(store.state.conversations[200].name, "Cached 201");
            assert.deepEqual(
                store.state.nextConversationCursor,
                activityConversationCursor(250, conversationActivityAt(250))
            );
            assert.ok(store.state.conversationsHaveMore);
            assert.strictEqual(store.state.selectedChannelId, 225);

            store.call = async (_method, _args, kwargs) => {
                assert.deepEqual(
                    kwargs.cursor,
                    activityConversationCursor(250, conversationActivityAt(250)),
                    "load more continues after the preserved pagination frontier"
                );
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: [{channel_id: 251, name: "Next page"}],
                    has_more: false,
                    next_cursor: false,
                    total: false,
                };
            };
            assert.strictEqual(await store.loadMoreConversations(), true);
            assert.strictEqual(store.state.conversations.length, 251);
            assert.strictEqual(store.state.conversations[250].channel_id, 251);
            assert.notOk(store.state.conversationsHaveMore);
        }
    );

    QUnit.test(
        "realtime refresh prepends new conversations without losing the cached tail",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.conversations = Array.from({length: 250}, (_value, index) => ({
                channel_id: index + 1,
                name: `Cached ${index + 1}`,
                last_activity_at: conversationActivityAt(index + 1),
            }));
            store.state.conversationsHaveMore = true;
            store.state.nextConversationCursor = activityConversationCursor(
                250,
                conversationActivityAt(250)
            );
            store.state.selectedChannelId = 250;
            let page = 0;
            store.call = async (_method, _args, kwargs) => {
                page += 1;
                const ids =
                    page === 1
                        ? [
                              1001,
                              ...Array.from({length: 99}, (_value, index) => index + 1),
                          ]
                        : Array.from(
                              {length: kwargs.limit},
                              (_value, index) => index + 100
                          );
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: ids.map((channelId) => ({
                        channel_id: channelId,
                        name: `Fresh ${channelId}`,
                        last_activity_at:
                            channelId === 1001
                                ? conversationActivityAt(0)
                                : conversationActivityAt(channelId),
                    })),
                    has_more: true,
                    next_cursor: activityConversationCursor(
                        ids[ids.length - 1],
                        conversationActivityAt(ids[ids.length - 1])
                    ),
                    total: page === 1 ? 501 : false,
                };
            };

            assert.strictEqual(
                await store.refreshLoadedConversations({silent: true}),
                true
            );
            assert.strictEqual(store.state.conversations.length, 251);
            assert.strictEqual(store.state.conversations[0].channel_id, 1001);
            assert.deepEqual(
                store.state.conversations.slice(1).map((item) => item.channel_id),
                Array.from({length: 250}, (_value, index) => index + 1),
                "new rows precede the complete previously loaded frontier"
            );
            assert.deepEqual(
                store.state.nextConversationCursor,
                activityConversationCursor(250, conversationActivityAt(250))
            );
            assert.strictEqual(store.state.selectedChannelId, 250);
        }
    );

    QUnit.test(
        "realtime refresh does not restore an item removed inside the fresh prefix",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.conversations = Array.from({length: 250}, (_value, index) => ({
                channel_id: index + 1,
                name: `Cached ${index + 1}`,
                last_activity_at: conversationActivityAt(index + 1),
            }));
            store.state.conversationsHaveMore = true;
            store.state.nextConversationCursor = activityConversationCursor(
                250,
                conversationActivityAt(250)
            );
            const freshIds = [
                ...Array.from({length: 49}, (_value, index) => index + 1),
                ...Array.from({length: 151}, (_value, index) => index + 51),
            ];
            let page = 0;
            store.call = async (_method, _args, kwargs) => {
                const start = page * 100;
                page += 1;
                const ids = freshIds.slice(start, start + kwargs.limit);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: ids.map((channelId) => ({
                        channel_id: channelId,
                        name: `Fresh ${channelId}`,
                        last_activity_at: conversationActivityAt(channelId),
                    })),
                    has_more: true,
                    next_cursor: activityConversationCursor(
                        ids[ids.length - 1],
                        conversationActivityAt(ids[ids.length - 1])
                    ),
                    total: page === 1 ? 499 : false,
                };
            };

            assert.strictEqual(
                await store.refreshLoadedConversations({silent: true}),
                true
            );
            assert.notOk(
                store.state.conversations.some((item) => item.channel_id === 50),
                "a resolved or newly inaccessible prefix item stays removed"
            );
            assert.strictEqual(store.state.conversations.length, 249);
            assert.strictEqual(store.state.conversations[199].channel_id, 201);
            assert.strictEqual(store.state.conversations[200].channel_id, 202);
            assert.strictEqual(store.state.conversations[248].channel_id, 250);
            assert.deepEqual(
                store.state.nextConversationCursor,
                activityConversationCursor(250, conversationActivityAt(250))
            );
        }
    );

    QUnit.test(
        "realtime refresh uses the server cursor when a deep cached row moves up",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.conversations = Array.from({length: 250}, (_value, index) => ({
                channel_id: index + 1,
                name: `Cached ${index + 1}`,
                last_activity_at: conversationActivityAt(index + 1),
            }));
            store.state.conversationsHaveMore = true;
            store.state.nextConversationCursor = activityConversationCursor(
                250,
                conversationActivityAt(250)
            );
            store.state.selectedChannelId = 220;
            const freshIds = [
                ...Array.from({length: 199}, (_value, index) => index + 1),
                230,
            ];
            let page = 0;
            store.call = async (_method, _args, kwargs) => {
                const start = page * 100;
                page += 1;
                const ids = freshIds.slice(start, start + kwargs.limit);
                const lastId = ids[ids.length - 1];
                const lastActivityAt =
                    lastId === 230
                        ? conversationActivityAt(199.5)
                        : conversationActivityAt(lastId);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: ids.map((channelId) => ({
                        channel_id: channelId,
                        name: `Fresh ${channelId}`,
                        last_activity_at:
                            channelId === 230
                                ? conversationActivityAt(199.5)
                                : conversationActivityAt(channelId),
                    })),
                    has_more: true,
                    next_cursor: activityConversationCursor(lastId, lastActivityAt),
                    total: page === 1 ? 500 : false,
                };
            };

            assert.strictEqual(
                await store.refreshLoadedConversations({silent: true}),
                true
            );
            assert.deepEqual(
                store.state.conversations.map((item) => item.channel_id),
                [
                    ...Array.from({length: 199}, (_value, index) => index + 1),
                    230,
                    ...Array.from({length: 30}, (_value, index) => index + 200),
                    ...Array.from({length: 20}, (_value, index) => index + 231),
                ],
                "the moved row does not truncate IDs 200 through 229 from the tail"
            );
            assert.deepEqual(
                store.state.nextConversationCursor,
                activityConversationCursor(250, conversationActivityAt(250))
            );
            assert.strictEqual(store.state.selectedChannelId, 220);
        }
    );

    QUnit.test(
        "realtime refresh reanchors honestly when the fresh prefix has no overlap",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.conversations = Array.from({length: 250}, (_value, index) => ({
                channel_id: index + 1,
                name: `Cached ${index + 1}`,
                last_activity_at: conversationActivityAt(index + 250),
            }));
            store.state.conversationsHaveMore = true;
            store.state.nextConversationCursor = activityConversationCursor(
                250,
                conversationActivityAt(499)
            );
            let page = 0;
            store.call = async (_method, _args, kwargs) => {
                const start = page * 100;
                page += 1;
                const ids = Array.from(
                    {length: kwargs.limit},
                    (_value, index) => index + start + 1001
                );
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: ids.map((channelId, index) => ({
                        channel_id: channelId,
                        name: `Fresh ${channelId}`,
                        last_activity_at: conversationActivityAt(start + index),
                    })),
                    has_more: true,
                    next_cursor: activityConversationCursor(
                        ids[ids.length - 1],
                        conversationActivityAt(start + ids.length - 1)
                    ),
                    total: page === 1 ? 1000 : false,
                };
            };

            assert.strictEqual(
                await store.refreshLoadedConversations({silent: true}),
                true
            );
            assert.deepEqual(
                store.state.conversations.map((item) => item.channel_id),
                Array.from({length: 200}, (_value, index) => index + 1001)
            );
            assert.deepEqual(
                store.state.nextConversationCursor,
                activityConversationCursor(1200, conversationActivityAt(199))
            );
            assert.ok(store.state.conversationsHaveMore);
        }
    );

    QUnit.test(
        "fences stale conversation loads and merges the next page by channel",
        async (assert) => {
            const pending = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.conversations = [
                {channel_id: 10, name: "Selected before refresh"},
            ];
            store.state.selectedChannelId = 10;
            store.state.messages = [{message_id: 1, body_text: "Cached"}];
            store.call = (method, _args, kwargs) => {
                assert.strictEqual(method, "list_conversations");
                return new Promise((resolve) => pending.push({kwargs, resolve}));
            };

            const staleLoad = store.loadConversations({reset: true});
            const currentLoad = store.loadConversations({reset: true});
            pending[1].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                items: [{channel_id: 20, name: "Current"}],
                has_more: true,
                next_cursor: activityConversationCursor(20, "2026-08-21 12:00:00"),
                total: 2,
            });
            assert.strictEqual(await currentLoad, true);
            pending[0].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                items: [{channel_id: 10, name: "Stale"}],
                has_more: false,
                next_cursor: false,
                total: 1,
            });
            assert.strictEqual(await staleLoad, false);
            assert.deepEqual(store.state.conversations, [
                {channel_id: 20, name: "Current"},
            ]);
            assert.strictEqual(
                store.state.selectedChannelId,
                false,
                "a non-silent reset clears a selection absent from the current page"
            );
            assert.deepEqual(store.state.messages, []);

            store.call = async (method, _args, kwargs) => {
                assert.strictEqual(method, "list_conversations");
                assert.deepEqual(
                    kwargs.cursor,
                    activityConversationCursor(20, "2026-08-21 12:00:00")
                );
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    items: [
                        {channel_id: 20, name: "Current updated"},
                        {channel_id: 30, name: "Next"},
                    ],
                    has_more: false,
                    next_cursor: false,
                    total: 2,
                };
            };
            assert.strictEqual(await store.loadConversations({reset: false}), true);
            assert.deepEqual(store.state.conversations, [
                {channel_id: 20, name: "Current updated"},
                {channel_id: 30, name: "Next"},
            ]);
            assert.strictEqual(store.state.listPhase, "ready");
        }
    );

    QUnit.test(
        "anchors the initial timeline at the first unread and pages forward",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.conversations = [
                {channel_id: 10, unread_count: 47, first_unread_message_id: 101},
            ];
            const calls = [];
            store.call = async (method, args, kwargs) => {
                calls.push({method, args, kwargs});
                if (calls.length === 1) {
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        channel_id: 10,
                        items: [
                            {message_id: 101, body_text: "First unread"},
                            {message_id: 102, body_text: "Next unread"},
                        ],
                        anchor_message_id: 101,
                        has_more: true,
                        next_before_message_id: 101,
                        has_more_forward: true,
                        next_after_chronological_message_id: 102,
                        latest_received_message_id: 103,
                    };
                }
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 10,
                    items: [{message_id: 103, body_text: "Newest unread"}],
                    has_more: false,
                    next_before_message_id: false,
                    has_more_forward: false,
                    next_after_chronological_message_id: 103,
                    latest_received_message_id: 103,
                };
            };

            assert.ok(await store.loadTimeline({reset: true}));
            assert.deepEqual(calls[0], {
                method: "get_timeline",
                args: [10],
                kwargs: {
                    before_message_id: false,
                    anchor_message_id: 101,
                    limit: 100,
                },
            });
            assert.strictEqual(store.state.timelineFirstUnreadMessageId, 101);
            assert.ok(store.state.timelineHasMoreForward);
            assert.strictEqual(store.state.nextAfterChronologicalMessageId, 102);
            assert.strictEqual(
                calls.length,
                1,
                "opening does not mark the unread tail seen"
            );

            assert.ok(await store.loadNewerMessages());
            assert.deepEqual(calls[1], {
                method: "get_timeline",
                args: [10],
                kwargs: {
                    before_message_id: false,
                    after_chronological_message_id: 102,
                    limit: 100,
                },
            });
            assert.deepEqual(
                store.state.messages.map((message) => message.message_id),
                [101, 102, 103]
            );
            assert.notOk(store.state.timelineHasMoreForward);
            assert.strictEqual(
                store.timelineContiguousCursor,
                103,
                "forward paging advances the realtime continuity boundary"
            );
        }
    );

    QUnit.test(
        "chronological forward paging accepts decreasing IDs",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {removeEventListener: () => undefined},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            const older = {message_id: 300, date: "2026-09-07 12:00:00"};
            const current = {message_id: 100, date: "2026-09-08 12:00:00"};
            store.state.messages = [older];
            store.state.nextAfterChronologicalMessageId = 300;
            store.state.timelineHasMoreForward = true;
            store.call = async (method, _args, kwargs) => {
                assert.strictEqual(method, "get_timeline");
                assert.strictEqual(kwargs.after_chronological_message_id, 300);
                assert.notOk(kwargs.after_message_id);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 10,
                    items: [current],
                    has_more: false,
                    has_more_forward: false,
                    next_after_chronological_message_id: 100,
                    latest_received_message_id: 300,
                };
            };
            assert.ok(await store.loadNewerMessages());
            assert.deepEqual(
                store.state.messages.map((item) => item.message_id),
                [300, 100]
            );
            assert.strictEqual(
                store.timelineContiguousCursor,
                300,
                "transport cursor remains monotonic"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "transport catchup handles backdated arrivals without disconnected history",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {removeEventListener: () => undefined},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.timelineRequest = 1;
            store.state.messages = [{message_id: 100, date: "2026-09-08 12:00:00"}];
            store.advanceTimelineContinuity(10, 100);
            store.call = async (_method, _args, kwargs) => {
                assert.strictEqual(kwargs.after_message_id, 100);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 10,
                    items: [
                        {message_id: 300, date: "2026-09-07 12:00:00"},
                        {message_id: 200, date: "2026-09-08 13:00:00"},
                    ],
                    has_more_forward: false,
                    next_after_message_id: 300,
                };
            };
            const page = await store.fetchForwardTimelinePage(1, 10, 100, 50);
            assert.strictEqual(
                page.nextAfterMessageId,
                300,
                "cursor uses max ingestion ID, not displayed tail"
            );
            store.applyForwardTimelineItems(page.items, 10, page.nextAfterMessageId);
            assert.deepEqual(
                store.state.messages.map((item) => item.message_id),
                [100, 200],
                "an arrival older than the loaded window remains available through older pagination"
            );
            assert.strictEqual(store.timelineContiguousCursor, 300);
            assert.ok(
                store.state.timelineHasMore,
                "older arrivals reopen pagination after the complete history was loaded"
            );
            assert.strictEqual(
                store.state.nextBeforeMessageId,
                100,
                "older pagination starts before the first retained chronological message"
            );
            assert.ok(
                store.timelineNeedsForwardRecovery(
                    {
                        items: [store.state.messages[1]],
                        has_more: false,
                        has_unloaded_received: true,
                    },
                    10
                ),
                "backdated arrivals trigger catchup even with overlapping newest messages"
            );
            store.destroy();
        }
    );

    QUnit.test(
        "mark seen clears unread at a lower-ID chronological tail",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {removeEventListener: () => undefined},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.messages = [
                {message_id: 300, date: "2026-09-07 12:00:00"},
                {message_id: 100, date: "2026-09-08 12:00:00"},
            ];
            store.state.conversations = [
                {
                    channel_id: 10,
                    unread_count: 1,
                    first_unread_message_id: 100,
                    last_activity_at: "2026-09-08 12:00:00",
                    last_message: {message_id: 100},
                },
            ];
            store.call = async () => ({channel_id: 10, message_id: 100});
            assert.ok(await store.markSeen(100));
            assert.strictEqual(store.selectedConversation.unread_count, 0);
            store.destroy();
        }
    );

    QUnit.test(
        "seen requests deduplicate the current selection but never reuse an obsolete selection token",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: new EventTarget(),
                notification: false,
            });
            Object.assign(store.state, {
                selectedChannelId: 10,
                timelineChannelId: 10,
                timelineFirstUnreadMessageId: 100,
                messages: [{message_id: 100}],
                conversations: [
                    openConversation({
                        channel_id: 10,
                        unread_count: 1,
                        first_unread_message_id: 100,
                    }),
                ],
            });
            const pending = [];
            store.call = (method, args) =>
                new Promise((resolve) => pending.push({method, args, resolve}));
            try {
                const first = store.markSeen(100);
                const repeated = store.markSeen(100);
                assert.strictEqual(
                    first,
                    repeated,
                    "simultaneous visibility signals share one acknowledgement"
                );
                assert.strictEqual(pending.length, 1);
                pending[0].resolve({channel_id: 10, message_id: 100});
                assert.ok(await first);
                assert.ok(await repeated);
                assert.strictEqual(store.selectedConversation.unread_count, 0);
                assert.ok(await store.markSeen(100));
                assert.strictEqual(
                    pending.length,
                    1,
                    "a confirmed tail does not generate repeated writes"
                );

                store.selectedConversation.unread_count = 1;
                store.selectedConversation.first_unread_message_id = 100;
                store.state.timelineFirstUnreadMessageId = 100;
                const obsolete = store.markSeen(100);
                assert.strictEqual(
                    pending.length,
                    2,
                    "marking unread again allows a fresh acknowledgement of the same tail"
                );
                store.cancelSeenRetry();
                store.state.selectedChannelId = 20;
                store.cancelSeenRetry();
                store.state.selectedChannelId = 10;
                const current = store.markSeen(100);
                assert.notStrictEqual(obsolete, current);
                assert.strictEqual(pending.length, 3);
                pending[1].resolve({channel_id: 10, message_id: 100});
                assert.notOk(await obsolete);
                assert.strictEqual(
                    store.selectedConversation.unread_count,
                    1,
                    "an obsolete response cannot clear the current selection"
                );
                pending[2].resolve({channel_id: 10, message_id: 100});
                assert.ok(await current);
                assert.strictEqual(store.selectedConversation.unread_count, 0);
                assert.strictEqual(store.pendingSeenRequests.size, 0);
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "invalid read confirmations and newer known messages preserve unread state",
        async (assert) => {
            const retries = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: new EventTarget(),
                notification: false,
                realtimeTimer: {
                    setTimeout: (callback) => {
                        retries.push(callback);
                        return retries.length;
                    },
                    clearTimeout: () => undefined,
                },
            });
            Object.assign(store.state, {
                selectedChannelId: 10,
                timelineChannelId: 10,
                timelineFirstUnreadMessageId: 100,
                messages: [{message_id: 100}],
                conversations: [
                    openConversation({
                        channel_id: 10,
                        unread_count: 1,
                        first_unread_message_id: 100,
                        last_activity_at: "2026-09-09 12:00:00",
                        last_message: {message_id: 100},
                    }),
                ],
            });
            store.state.messages[0].date = "2026-09-09 12:00:00";
            try {
                for (const response of [
                    true,
                    undefined,
                    {channel_id: 20, message_id: 100},
                    {channel_id: 10, message_id: 99},
                ]) {
                    store.call = async () => response;
                    assert.notOk(await store.markSeen(100));
                    assert.strictEqual(store.selectedConversation.unread_count, 1);
                    assert.strictEqual(store.state.timelineFirstUnreadMessageId, 100);
                }
                assert.strictEqual(
                    retries.length,
                    4,
                    "invalid acknowledgements retain the retry policy"
                );
                let resolveSeen = null;
                store.call = () =>
                    new Promise((resolve) => {
                        resolveSeen = resolve;
                    });
                const synchronizations = [];
                store.scheduleSynchronization = (reconnect, refreshTimeline) =>
                    synchronizations.push({reconnect, refreshTimeline});
                const seen = store.markSeen(100);
                store.selectedConversation.last_message = {message_id: 99};
                store.selectedConversation.last_activity_at = "2026-09-09 12:01:00";
                store.selectedConversation.unread_count = 2;
                resolveSeen({channel_id: 10, message_id: 100});
                assert.ok(await seen);
                assert.strictEqual(
                    store.selectedConversation.unread_count,
                    2,
                    "a valid acknowledgement of an older loaded tail cannot clear a newer known arrival"
                );
                assert.strictEqual(store.state.timelineFirstUnreadMessageId, 100);
                assert.deepEqual(synchronizations, [
                    {reconnect: false, refreshTimeline: true},
                ]);
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "retries a failed seen pointer before clearing unread state",
        async (assert) => {
            const retryCallbacks = [];
            const realtimeTimer = {
                setTimeout(callback, delay) {
                    retryCallbacks.push({callback, delay});
                    return retryCallbacks.length;
                },
                clearTimeout() {
                    return undefined;
                },
            };
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
                realtimeTimer,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.timelineFirstUnreadMessageId = 101;
            store.state.messages = [{message_id: 101}, {message_id: 102}];
            store.state.conversations = [
                {
                    channel_id: 10,
                    state: "open",
                    unread_count: 2,
                    first_unread_message_id: 101,
                },
            ];
            let calls = 0;
            store.call = async (method, args) => {
                assert.strictEqual(method, "mark_seen");
                assert.deepEqual(args, [10, 102]);
                calls += 1;
                if (calls === 1) {
                    throw new Error("temporary transport failure");
                }
                return {channel_id: 10, message_id: 102};
            };

            assert.notOk(await store.markSeen(102));
            assert.strictEqual(store.selectedConversation.unread_count, 2);
            assert.strictEqual(retryCallbacks.length, 1);
            assert.strictEqual(retryCallbacks[0].delay, 1000);
            await retryCallbacks[0].callback();
            assert.strictEqual(calls, 2);
            assert.strictEqual(store.selectedConversation.unread_count, 0);
            assert.notOk(store.state.timelineFirstUnreadMessageId);
        }
    );

    QUnit.test(
        "removes a selected conversation that no longer matches the state tab",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.filters.states = ["open"];
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.conversations = [{channel_id: 10, state: "open"}];
            store.state.messages = [{message_id: 1}];
            let listReloaded = false;
            store.call = async (method) => {
                assert.strictEqual(method, "get_conversation");
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    item: {channel_id: 10, state: "resolved"},
                };
            };
            store.loadConversations = async ({reset, selectFirst}) => {
                listReloaded = reset && selectFirst;
                return true;
            };

            assert.ok(await store.refreshSelectedConversation({silent: true}));
            assert.notOk(store.state.selectedChannelId);
            assert.deepEqual(store.state.conversations, []);
            assert.deepEqual(store.state.messages, []);
            assert.ok(listReloaded, "the active tab is reloaded after removal");
        }
    );

    QUnit.test(
        "fences stale timelines and prepends older messages without regression",
        async (assert) => {
            const pending = [];
            const seen = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.call = (method, args, kwargs) => {
                if (method === "mark_seen") {
                    seen.push(args);
                    return Promise.resolve(true);
                }
                assert.strictEqual(method, "get_timeline");
                return new Promise((resolve) => pending.push({args, kwargs, resolve}));
            };

            store.state.selectedChannelId = 10;
            const staleLoad = store.loadTimeline({reset: true});
            store.state.selectedChannelId = 20;
            const currentLoad = store.loadTimeline({reset: true});
            pending[1].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 20,
                items: [{message_id: 200, body_text: "Current"}],
                has_more: true,
                next_before_message_id: 150,
            });
            assert.strictEqual(await currentLoad, true);
            pending[0].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 10,
                items: [{message_id: 100, body_text: "Wrong conversation"}],
                has_more: false,
                next_before_message_id: false,
            });
            assert.strictEqual(await staleLoad, false);
            assert.deepEqual(
                store.state.messages.map(({message_id, body_text}) => ({
                    message_id,
                    body_text,
                })),
                [{message_id: 200, body_text: "Current"}]
            );

            store.call = async (method, args, kwargs) => {
                if (method === "mark_seen") {
                    seen.push(args);
                    return true;
                }
                assert.strictEqual(method, "get_timeline");
                assert.deepEqual(args, [20]);
                assert.strictEqual(kwargs.before_message_id, 150);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 20,
                    items: [
                        {message_id: 100, body_text: "Older"},
                        {message_id: 200, body_text: "Stale duplicate"},
                    ],
                    has_more: false,
                    next_before_message_id: false,
                };
            };
            assert.strictEqual(await store.loadTimeline({reset: false}), true);
            assert.deepEqual(
                store.state.messages.map(({message_id, body_text}) => ({
                    message_id,
                    body_text,
                })),
                [
                    {message_id: 100, body_text: "Older"},
                    {message_id: 200, body_text: "Current"},
                ]
            );
            assert.deepEqual(
                seen,
                [],
                "fetching messages does not prove they were visible"
            );
            assert.strictEqual(store.state.timelinePhase, "ready");
        }
    );

    QUnit.test(
        "merges a live latest-page refresh without losing history or its cursor",
        async (assert) => {
            const calls = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.conversations = [{channel_id: 10, unread_count: 1}];
            store.state.messages = Array.from({length: 150}, (_item, index) => ({
                message_id: index + 51,
                body_text: `Existing ${index + 51}`,
            }));
            store.timelineContiguousChannelId = 10;
            store.timelineContiguousCursor = 200;
            store.state.timelineHasMore = true;
            store.state.nextBeforeMessageId = 51;
            store.state.timelinePhase = "ready";
            store.call = async (method, args, kwargs) => {
                calls.push({method, args, kwargs});
                if (method === "mark_seen") {
                    return {schema_version: SUPPORTED_SCHEMA_VERSION};
                }
                assert.strictEqual(method, "get_timeline");
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 10,
                    items: Array.from({length: 100}, (_item, index) => ({
                        message_id: index + 102,
                        body_text:
                            index === 98
                                ? "Existing 200 refreshed"
                                : `Latest ${index + 102}`,
                    })),
                    has_more: true,
                    next_before_message_id: 102,
                };
            };

            assert.strictEqual(await store.refreshLatestTimeline(), true);
            assert.strictEqual(calls[0].method, "get_timeline");
            assert.deepEqual(calls[0].args, [10]);
            assert.deepEqual(calls[0].kwargs, {
                before_message_id: false,
                limit: 100,
                known_received_message_id: 200,
            });
            assert.strictEqual(
                calls.length,
                1,
                "a live refresh does not mark unseen messages while the agent reads history"
            );
            assert.strictEqual(store.state.messages.length, 151);
            assert.strictEqual(store.state.messages[0].message_id, 51);
            assert.strictEqual(store.state.messages[150].message_id, 201);
            assert.strictEqual(
                store.state.messages[149].body_text,
                "Existing 200 refreshed"
            );
            assert.ok(store.state.timelineHasMore);
            assert.strictEqual(
                store.state.nextBeforeMessageId,
                51,
                "the oldest loaded-page cursor is not replaced by the latest page"
            );
        }
    );

    QUnit.test(
        "fills a forward gap despite a local high-ID island and applies a fresh head",
        async (assert) => {
            const calls = [];
            let headCalls = 0;
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.messages = Array.from({length: 50}, (_item, index) => ({
                message_id: index + 1,
                body_text: `Existing ${index + 1}`,
            }));
            store.state.messages.push({
                message_id: 300,
                body_text: "Concurrent local send",
            });
            store.timelineContiguousChannelId = 10;
            store.timelineContiguousCursor = 50;
            store.state.timelineHasMore = true;
            store.state.nextBeforeMessageId = 1;
            store.state.timelinePhase = "ready";
            store.call = async (method, args, kwargs) => {
                assert.strictEqual(method, "get_timeline");
                assert.deepEqual(args, [10]);
                calls.push(kwargs);
                if (!kwargs.after_message_id) {
                    headCalls += 1;
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        channel_id: 10,
                        items: Array.from({length: 100}, (_item, index) => ({
                            message_id: index + 201,
                            body_text: `${headCalls === 1 ? "Stale" : "Fresh"} latest ${
                                index + 201
                            }`,
                        })),
                        has_more: true,
                        next_before_message_id: 201,
                        has_more_forward: false,
                        next_after_message_id: false,
                    };
                }
                const start = kwargs.after_message_id + 1;
                const end = Math.min(start + 99, 300);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 10,
                    items: Array.from({length: end - start + 1}, (_item, index) => ({
                        message_id: start + index,
                        body_text: `Delta ${start + index}`,
                    })),
                    has_more: false,
                    next_before_message_id: false,
                    has_more_forward: end < 300,
                    next_after_message_id: end,
                };
            };

            assert.strictEqual(await store.refreshLatestTimeline(), true);
            assert.strictEqual(
                calls.length,
                5,
                "one detection head, three deltas and one fresh final head"
            );
            assert.strictEqual(headCalls, 2);
            assert.deepEqual(calls[0], {
                before_message_id: false,
                limit: 100,
                known_received_message_id: 50,
            });
            assert.deepEqual(calls.slice(1, 4), [
                {after_message_id: 50, limit: 100},
                {after_message_id: 150, limit: 100},
                {after_message_id: 250, limit: 100},
            ]);
            assert.deepEqual(calls[4], {
                before_message_id: false,
                limit: 100,
                known_received_message_id: 300,
            });
            assert.strictEqual(store.state.messages.length, 300);
            assert.strictEqual(store.state.messages[0].message_id, 1);
            assert.strictEqual(store.state.messages[299].message_id, 300);
            assert.strictEqual(
                store.state.messages[250].body_text,
                "Fresh latest 251",
                "a stale detection head cannot overwrite the newer final snapshot"
            );
            assert.ok(store.state.timelineHasMore);
            assert.strictEqual(
                store.state.nextBeforeMessageId,
                1,
                "older pagination keeps the original loaded boundary"
            );
        }
    );

    QUnit.test(
        "bounds aggregate forward catch-up and safely reanchors a huge backlog",
        async (assert) => {
            const scheduled = [];
            let deltaCalls = 0;
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.messages = [{message_id: 1, body_text: "Loaded"}];
            store.timelineContiguousChannelId = 10;
            store.timelineContiguousCursor = 1;
            store.state.timelineHasMore = true;
            store.state.nextBeforeMessageId = 1;
            store.state.timelinePhase = "ready";
            store.scheduleSynchronization = (...args) => scheduled.push(args);
            store.call = async (_method, _args, kwargs) => {
                if (!kwargs.after_message_id) {
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        channel_id: 10,
                        items: [{message_id: 5000, body_text: "Disconnected head"}],
                        has_more: true,
                        next_before_message_id: 5000,
                        has_more_forward: false,
                        next_after_message_id: false,
                    };
                }
                deltaCalls += 1;
                const messageId = kwargs.after_message_id + 1;
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 10,
                    items: [{message_id: messageId, body_text: `Delta ${messageId}`}],
                    has_more: false,
                    next_before_message_id: false,
                    has_more_forward: true,
                    next_after_message_id: messageId,
                };
            };

            assert.strictEqual(await store.refreshLatestTimeline(), true);
            assert.strictEqual(
                deltaCalls,
                20,
                "automatic catch-up has a fixed aggregate page bound"
            );
            assert.deepEqual(scheduled, []);
            assert.deepEqual(
                store.state.messages.map(({message_id, body_text}) => ({
                    message_id,
                    body_text,
                })),
                [{message_id: 5000, body_text: "Disconnected head"}]
            );
            assert.strictEqual(
                store.state.nextBeforeMessageId,
                5000,
                "older history remains available from the fresh head"
            );
            assert.strictEqual(store.timelineForwardChannelId, false);
            assert.strictEqual(store.timelineForwardCursor, false);
            assert.strictEqual(store.timelineForwardPageCount, 0);
            assert.strictEqual(store.timelineContiguousChannelId, 10);
            assert.strictEqual(store.timelineContiguousCursor, 5000);
        }
    );

    QUnit.test(
        "blocks older pagination while a forward recovery request is in flight",
        async (assert) => {
            let resolveDelta = null;
            let olderCalls = 0;
            const scheduled = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.messages = [{message_id: 1, body_text: "Loaded"}];
            store.state.timelineHasMore = true;
            store.state.nextBeforeMessageId = 1;
            store.state.timelinePhase = "ready";
            store.timelineContiguousChannelId = 10;
            store.timelineContiguousCursor = 1;
            store.scheduleSynchronization = (...args) => scheduled.push(args);
            store.call = async (_method, _args, kwargs) => {
                if (kwargs.before_message_id) {
                    olderCalls += 1;
                }
                if (kwargs.after_message_id) {
                    return new Promise((resolve) => {
                        resolveDelta = resolve;
                    });
                }
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 10,
                    items: [{message_id: 200, body_text: "Disconnected head"}],
                    has_more: true,
                    next_before_message_id: 200,
                    has_more_forward: false,
                    next_after_message_id: false,
                };
            };

            const refresh = store.refreshLatestTimeline();
            await Promise.resolve();
            await Promise.resolve();
            assert.strictEqual(store.timelineForwardCursor, 1);
            assert.strictEqual(typeof resolveDelta, "function");
            assert.strictEqual(await store.loadOlderMessages(), false);
            assert.strictEqual(olderCalls, 0);

            resolveDelta({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 10,
                items: [{message_id: 2, body_text: "Delta"}],
                has_more: false,
                next_before_message_id: false,
                has_more_forward: false,
                next_after_message_id: 2,
            });
            assert.strictEqual(await refresh, true);
            assert.deepEqual(scheduled, [[false, true]]);
        }
    );

    QUnit.test(
        "retries a silent forward failure from its fenced cursor within the cap",
        async (assert) => {
            const scheduled = [];
            const afterCursors = [];
            let failForward = true;
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.messages = [{message_id: 1, body_text: "Loaded"}];
            store.state.timelineHasMore = true;
            store.state.nextBeforeMessageId = 1;
            store.state.timelinePhase = "ready";
            store.timelineContiguousChannelId = 10;
            store.timelineContiguousCursor = 1;
            store.scheduleSynchronization = (...args) => scheduled.push(args);
            store.call = async (_method, _args, kwargs) => {
                if (kwargs.after_message_id) {
                    afterCursors.push(kwargs.after_message_id);
                    if (failForward) {
                        throw new Error("transient forward failure");
                    }
                    return {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        channel_id: 10,
                        items: [
                            {message_id: 2, body_text: "Recovered delta"},
                            {message_id: 200, body_text: "Recovered head"},
                        ],
                        has_more: false,
                        next_before_message_id: false,
                        has_more_forward: false,
                        next_after_message_id: 200,
                    };
                }
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 10,
                    items: [{message_id: 200, body_text: "Current head"}],
                    has_more: true,
                    next_before_message_id: 200,
                    has_more_forward: false,
                    next_after_message_id: false,
                };
            };

            assert.strictEqual(await store.refreshLatestTimeline(), false);
            assert.deepEqual(afterCursors, [1]);
            assert.deepEqual(scheduled, [[false, true]]);
            assert.strictEqual(store.timelineForwardChannelId, 10);
            assert.strictEqual(store.timelineForwardCursor, 1);
            assert.strictEqual(store.timelineForwardPageCount, 1);

            failForward = false;
            assert.strictEqual(await store.refreshLatestTimeline(), true);
            assert.deepEqual(
                afterCursors,
                [1, 1],
                "the retry resumes at the fenced cursor"
            );
            assert.strictEqual(store.timelineForwardChannelId, false);
            assert.strictEqual(store.timelineForwardCursor, false);
            assert.strictEqual(store.timelineForwardPageCount, 0);
            assert.ok(
                store.state.messages.some((item) => item.message_id === 2),
                "the retried delta is applied"
            );

            store.timelineForwardChannelId = 10;
            store.timelineForwardCursor = 200;
            store.timelineForwardPageCount = 20;
            store.call = async () => {
                throw new Error("persistent forward failure");
            };
            assert.strictEqual(await store.refreshLatestTimeline(), false);
            assert.deepEqual(
                scheduled,
                [[false, true]],
                "an exhausted recovery budget does not schedule another retry"
            );
            assert.strictEqual(store.timelineForwardCursor, 200);
            assert.strictEqual(store.timelineForwardPageCount, 20);
        }
    );

    QUnit.test(
        "an obsolete latest-page refresh cannot reanchor the current timeline",
        async (assert) => {
            const pending = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.messages = [{message_id: 101, body_text: "Cached"}];
            store.timelineContiguousChannelId = 10;
            store.timelineContiguousCursor = 101;
            store.state.timelineHasMore = true;
            store.state.nextBeforeMessageId = 101;
            store.state.timelinePhase = "ready";
            store.call = (method) => {
                assert.strictEqual(method, "get_timeline");
                return new Promise((resolve) => pending.push(resolve));
            };

            const obsoleteRefresh = store.refreshLatestTimeline();
            const currentRefresh = store.refreshLatestTimeline();
            pending[1]({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 10,
                items: [
                    {message_id: 101, body_text: "Cached refreshed"},
                    {message_id: 701, body_text: "Current head"},
                ],
                has_more: true,
                next_before_message_id: 101,
            });
            assert.strictEqual(await currentRefresh, true);
            pending[0]({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 10,
                items: [
                    {message_id: 101, body_text: "Obsolete cached"},
                    {message_id: 401, body_text: "Obsolete head"},
                ],
                has_more: true,
                next_before_message_id: 101,
            });
            assert.strictEqual(await obsoleteRefresh, false);
            assert.deepEqual(
                store.state.messages.map(({message_id, body_text}) => ({
                    message_id,
                    body_text,
                })),
                [
                    {message_id: 101, body_text: "Cached refreshed"},
                    {message_id: 701, body_text: "Current head"},
                ],
                "only the latest request may replace the loaded window"
            );
            assert.strictEqual(store.state.nextBeforeMessageId, 101);
        }
    );

    QUnit.test(
        "never merges a realtime refresh into the previous conversation",
        async (assert) => {
            const pending = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.conversations = [
                {channel_id: 10, name: "Conversation A"},
                {channel_id: 20, name: "Conversation B"},
            ];
            store.state.selectedChannelId = 10;
            store.state.timelineChannelId = 10;
            store.state.messages = [{message_id: 10, body_text: "Only A"}];
            store.call = (method, args) => {
                if (method === "mark_seen") {
                    return Promise.resolve({schema_version: SUPPORTED_SCHEMA_VERSION});
                }
                assert.strictEqual(method, "get_timeline");
                return new Promise((resolve) => pending.push({args, resolve}));
            };

            const initialB = store.selectConversation(20);
            assert.deepEqual(store.state.messages, [], "A is cleared immediately");
            const liveB = store.refreshLatestTimeline();
            assert.deepEqual(
                pending.map(({args}) => args),
                [[20], [20]],
                "both requests are stamped for B"
            );

            pending[1].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 20,
                items: [{message_id: 20, body_text: "Only B"}],
                has_more: false,
                next_before_message_id: false,
            });
            assert.strictEqual(await liveB, true);
            pending[0].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 20,
                items: [{message_id: 21, body_text: "Stale B reset"}],
                has_more: false,
                next_before_message_id: false,
            });
            assert.strictEqual(await initialB, false);
            assert.strictEqual(store.state.timelineChannelId, 20);
            assert.deepEqual(
                store.state.messages.map(({message_id, body_text}) => ({
                    message_id,
                    body_text,
                })),
                [{message_id: 20, body_text: "Only B"}]
            );
        }
    );

    QUnit.test(
        "moves a current invalid attribution response to error and ignores a stale failure",
        async (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.resetAttribution(10);
            store.call = async () => attributionEnvelope(10, [{malformed: true}]);

            assert.notOk(await store.loadAttribution());
            assert.strictEqual(store.state.attribution.channelId, 10);
            assert.strictEqual(
                store.state.attribution.phase,
                "error",
                "the current malformed response does not remain loading"
            );

            let resolveStale = null;
            store.resetAttribution(10);
            store.call = () =>
                new Promise((resolve) => {
                    resolveStale = resolve;
                });
            const staleRequest = store.loadAttribution();
            store.state.selectedChannelId = 20;
            store.resetAttribution(20);
            resolveStale({schema_version: SUPPORTED_SCHEMA_VERSION + 1});

            assert.notOk(await staleRequest);
            assert.strictEqual(store.state.attribution.channelId, 20);
            assert.strictEqual(
                store.state.attribution.phase,
                "idle",
                "a stale failure cannot change the new conversation state"
            );
        }
    );

    QUnit.test(
        "clears cached attribution when the selected conversation loses capability",
        (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.conversations = [
                {
                    channel_id: 10,
                    state: "open",
                    conversation_type: "direct",
                    capabilities: {view_attribution: true},
                },
            ];
            store.state.attribution = {
                channelId: 10,
                phase: "ready",
                enabled: true,
                items: [
                    attributionItem(
                        "aaaaaaaa-1111-4111-8111-111111111111",
                        "Campanha privada"
                    ),
                ],
                hasMore: false,
                nextCursor: false,
            };

            store.replaceConversation({
                channel_id: 10,
                state: "open",
                conversation_type: "direct",
                capabilities: {view_attribution: false},
            });

            assert.notOk(store.canViewAttribution());
            assert.deepEqual(store.state.attribution, {
                channelId: 10,
                phase: "idle",
                enabled: false,
                items: [],
                hasMore: false,
                nextCursor: false,
            });
        }
    );

    QUnit.test(
        "keeps loaded attribution pages during a realtime head refresh",
        async (assert) => {
            const refs = {
                newest: "99999999-1111-4111-8111-111111111111",
                a: "aaaaaaaa-1111-4111-8111-111111111111",
                b: "bbbbbbbb-1111-4111-8111-111111111111",
                c: "cccccccc-1111-4111-8111-111111111111",
                d: "dddddddd-1111-4111-8111-111111111111",
                e: "eeeeeeee-1111-4111-8111-111111111111",
                f: "ffffffff-1111-4111-8111-111111111111",
            };
            const responses = [
                attributionEnvelope(
                    10,
                    [
                        attributionItem(refs.a, "A antiga"),
                        attributionItem(refs.b, "B"),
                        attributionItem(refs.c, "C"),
                    ],
                    {has_more: true, next_cursor: refs.c}
                ),
                attributionEnvelope(
                    10,
                    [
                        attributionItem(refs.d, "D"),
                        attributionItem(refs.e, "E"),
                        attributionItem(refs.f, "F"),
                    ],
                    {has_more: true, next_cursor: refs.f}
                ),
                attributionEnvelope(
                    10,
                    [
                        attributionItem(refs.newest, "Nova"),
                        attributionItem(refs.a, "A enriquecida"),
                        attributionItem(refs.b, "B"),
                    ],
                    {has_more: true, next_cursor: refs.b}
                ),
            ];
            const cursors = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.resetAttribution(10);
            store.call = (_method, _args, kwargs) => {
                cursors.push(kwargs.cursor);
                return Promise.resolve(responses.shift());
            };

            assert.ok(await store.loadAttribution());
            assert.ok(await store.loadAttribution({append: true}));
            assert.ok(await store.loadAttribution({preserve: true, silent: true}));

            assert.deepEqual(cursors, [false, refs.c, false]);
            assert.deepEqual(
                store.state.attribution.items.map((item) => item.public_ref),
                [refs.newest, refs.a, refs.b, refs.c, refs.d, refs.e, refs.f],
                "the refreshed head is merged before every previously loaded page"
            );
            assert.strictEqual(
                store.state.attribution.items.find((item) => item.public_ref === refs.a)
                    .utm_campaign,
                "A enriquecida",
                "the refreshed copy wins deduplication"
            );
            assert.strictEqual(store.state.attribution.nextCursor, refs.f);
            assert.ok(store.state.attribution.hasMore);
        }
    );

    QUnit.test(
        "refreshes attribution from the bus only while its gated panel is open",
        (assert) => {
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.selectedChannelId = 10;
            store.state.conversations = [
                {
                    channel_id: 10,
                    conversation_type: "direct",
                    capabilities: {view_attribution: true},
                },
            ];
            const calls = [];
            store.loadAttribution = (options) => {
                calls.push(options);
                return Promise.resolve(true);
            };
            const notification = {
                event_type: "attribution_updated",
                channel_id: 10,
            };

            store.state.detailsOpen = false;
            assert.ok(store.handleAttributionNotification(notification));
            assert.deepEqual(calls, [], "a closed panel stays lazy");

            store.state.detailsOpen = true;
            assert.ok(store.handleAttributionNotification(notification));
            assert.deepEqual(calls, [{silent: true, preserve: true}]);

            store.state.conversations[0].capabilities.view_attribution = false;
            assert.ok(store.handleAttributionNotification(notification));
            assert.strictEqual(calls.length, 1, "a revoked capability blocks refresh");
        }
    );

    QUnit.test(
        "never applies a delayed attribution response to another conversation",
        async (assert) => {
            const pending = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            const item = (publicRef, campaign) => ({
                public_ref: publicRef,
                touchpoint_type: "paid_ad_click",
                evidence_level: "provider_asserted",
                network: "meta",
                source_platform: "instagram",
                source_type: "ad",
                entry_point_source: "ctwa_ad",
                entry_point_app: "instagram",
                utm_source: "",
                utm_medium: "",
                utm_campaign: campaign,
                creative_media_type: "image",
                show_ad_attribution: true,
                occurred_at: "2026-08-25 14:30:00",
            });
            store.call = (method, args) => {
                assert.strictEqual(method, "get_attribution");
                return new Promise((resolve) => pending.push({args, resolve}));
            };

            store.state.selectedChannelId = 10;
            store.resetAttribution(10);
            const requestA = store.loadAttribution();
            store.state.selectedChannelId = 20;
            store.resetAttribution(20);
            const requestB = store.loadAttribution();

            pending[1].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 20,
                enabled: true,
                items: [item("bbbbbbbb-1111-4222-8333-444455556666", "Conversation B")],
                has_more: false,
                next_cursor: false,
            });
            assert.strictEqual(await requestB, true);
            pending[0].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 10,
                enabled: true,
                items: [item("aaaaaaaa-1111-4222-8333-444455556666", "Conversation A")],
                has_more: false,
                next_cursor: false,
            });
            assert.strictEqual(await requestA, false);
            assert.strictEqual(store.state.attribution.channelId, 20);
            assert.deepEqual(
                store.state.attribution.items.map(({utm_campaign}) => utm_campaign),
                ["Conversation B"]
            );
        }
    );

    QUnit.test("uses randomUUID when it returns a canonical identifier", (assert) => {
        const result = makeClientRequestId({
            randomUUID: () => "A0E1C2D3-4455-4677-8899-AABBCCDDEEFF",
        });

        assert.strictEqual(result, "a0e1c2d3-4455-4677-8899-aabbccddeeff");
    });

    QUnit.test(
        "builds canonical UUID v4 identifiers from injected fallbacks",
        (assert) => {
            const fromBytes = makeClientRequestId({
                randomUUID: () => "not-a-uuid",
                getRandomValues(bytes) {
                    for (let index = 0; index < bytes.length; index += 1) {
                        bytes[index] = index;
                    }
                    return bytes;
                },
            });
            const fromRandom = makeClientRequestId(() => 0);

            assert.strictEqual(fromBytes, "00010203-0405-4607-8809-0a0b0c0d0e0f");
            assert.strictEqual(fromRandom, "00000000-0000-4000-8000-000000000000");
            assert.ok(
                /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
                    fromBytes
                )
            );
        }
    );

    QUnit.test("normalizes productivity and quick reply contracts", (assert) => {
        const productivity = normalizeProductivity(
            {
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 42,
                activities: [{id: 8, summary: "Retornar"}],
                scheduled_messages: [{id: 9, body: "Olá"}],
                activity_types: [{id: 10, name: "Ligação"}],
                assignable_users: [{id: 11, name: "Ana"}],
            },
            42
        );

        assert.strictEqual(productivity.channelId, 42);
        assert.strictEqual(productivity.phase, "ready");
        assert.notOk("cases" in productivity);
        assert.deepEqual(productivity.activities, [{id: 8, summary: "Retornar"}]);
        assert.deepEqual(productivity.scheduledMessages, [{id: 9, body: "Olá"}]);
        assert.deepEqual(productivity.activityTypes, [{id: 10, name: "Ligação"}]);
        assert.deepEqual(productivity.assignableUsers, [{id: 11, name: "Ana"}]);

        assert.throws(
            () =>
                normalizeProductivity(
                    {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        channel_id: 43,
                    },
                    42
                ),
            /outra conversa/,
            "a late response can never replace another conversation's projection"
        );

        assert.deepEqual(
            normalizeQuickReplies(
                {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 42,
                    items: [
                        {
                            id: 3,
                            binding_id: 13,
                            scope: "team",
                            shortcut: "/horario",
                            body: "Atendemos até as 18h.",
                            description: "Horário comercial",
                        },
                    ],
                },
                42
            ),
            [
                {
                    id: 3,
                    bindingId: 13,
                    scope: "team",
                    shortcut: "/horario",
                    name: "/horario",
                    body: "Atendemos até as 18h.",
                    description: "Horário comercial",
                },
            ],
            "the provider shortcut becomes the visible palette label"
        );

        assert.throws(
            () =>
                normalizeQuickReplies(
                    {
                        schema_version: SUPPORTED_SCHEMA_VERSION,
                        channel_id: 43,
                        items: [],
                    },
                    42
                ),
            /outra conversa/,
            "a response for another conversation is rejected"
        );
    });

    QUnit.test(
        "scopes quick reply searches to the selected conversation",
        async (assert) => {
            const calls = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {capabilities: {quick_replies: true}};
            store.state.selectedChannelId = 42;
            store.call = async (method, args, kwargs) => {
                calls.push({method, args, kwargs});
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 42,
                    items: [
                        {
                            id: 3,
                            binding_id: 13,
                            scope: "channel",
                            shortcut: "/horario",
                            body: "Atendemos até as 18h.",
                            description: "Horário comercial",
                        },
                    ],
                };
            };

            assert.ok(await store.searchQuickReplies("  horario  ", 999));
            assert.deepEqual(calls, [
                {
                    method: "search_quick_replies",
                    args: [42],
                    kwargs: {query: "horario", limit: 20},
                },
            ]);
            assert.strictEqual(store.state.quickReplies.channelId, 42);
            assert.strictEqual(store.state.quickReplies.items[0].bindingId, 13);
            assert.strictEqual(store.state.quickReplies.items[0].scope, "channel");
        }
    );

    QUnit.test(
        "does not search quick replies without a selected conversation",
        async (assert) => {
            let callCount = 0;
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {capabilities: {quick_replies: true}};
            store.call = async () => {
                callCount += 1;
                throw new Error("RPC must not run");
            };

            assert.notOk(await store.searchQuickReplies("horario"));
            assert.strictEqual(callCount, 0);
            assert.strictEqual(store.state.quickReplies.channelId, false);
            assert.strictEqual(store.state.quickReplies.phase, "idle");
        }
    );

    QUnit.test(
        "never applies delayed quick replies to another conversation",
        async (assert) => {
            const pending = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {capabilities: {quick_replies: true}};
            store.call = (method, args) => {
                assert.strictEqual(method, "search_quick_replies");
                return new Promise((resolve) => pending.push({args, resolve}));
            };

            store.state.selectedChannelId = 10;
            store.resetQuickReplies(10);
            const requestA = store.searchQuickReplies("a");
            store.state.selectedChannelId = 20;
            store.resetQuickReplies(20);
            const requestB = store.searchQuickReplies("b");

            pending[1].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 20,
                items: [
                    {
                        id: 20,
                        binding_id: 120,
                        scope: "account",
                        shortcut: "/b",
                        body: "Conversation B",
                        description: "",
                    },
                ],
            });
            assert.ok(await requestB);
            pending[0].resolve({
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 10,
                items: [
                    {
                        id: 10,
                        binding_id: 110,
                        scope: "account",
                        shortcut: "/a",
                        body: "Conversation A",
                        description: "",
                    },
                ],
            });
            assert.notOk(await requestA);
            assert.deepEqual(
                pending.map(({args}) => args),
                [[10], [20]]
            );
            assert.strictEqual(store.state.quickReplies.channelId, 20);
            assert.strictEqual(
                store.state.quickReplies.items[0].body,
                "Conversation B"
            );
        }
    );

    QUnit.test("serializes every productivity inbox filter", (assert) => {
        const store = new ContactCenterStore({
            orm: {},
            busService: {},
            notification: false,
        });
        store.state.filters = {
            states: ["open", "resolved"],
            accountId: 4,
            query: "  Maria  ",
            responsibility: "mine",
            unreadOnly: true,
            conversationType: "direct",
            tagId: 12,
            activityTiming: "overdue",
        };

        assert.deepEqual(store.conversationFilters(), {
            states: ["open", "resolved"],
            account_id: 4,
            query: "Maria",
            responsibility: "mine",
            unread_only: true,
            conversation_type: "direct",
            tag_ids: [12],
            activity_timing: "overdue",
        });
    });

    QUnit.test("converts datetime-local to an explicit UTC instant", (assert) => {
        const localValue = "2030-01-02T03:04:05";
        const expected = new Date(2030, 0, 2, 3, 4, 5, 0).toISOString();

        assert.strictEqual(localDateTimeToOdooUtc(localValue), expected);
        assert.strictEqual(formatOdooUtcDateTime(new Date(expected)), expected);
        assert.ok(expected.endsWith("Z"), "the backend receives an explicit zone");
        assert.throws(
            () => localDateTimeToOdooUtc("2030-02-31T03:04"),
            TypeError,
            "calendar overflow is rejected"
        );
    });

    QUnit.test("describes every scheduled-message state explicitly", (assert) => {
        assert.deepEqual(scheduledMessageStateMeta("scheduled"), {
            key: "scheduled",
            label: "Agendada",
            icon: "fa fa-clock-o",
        });
        assert.strictEqual(scheduledMessageStateMeta("failed").label, "Falhou");
        assert.strictEqual(
            scheduledMessageStateMeta("unexpected").key,
            "unknown",
            "new backend states remain visible rather than looking pending"
        );
    });

    QUnit.test(
        "scheduled-message creation follows the conversation send policy",
        (assert) => {
            const composer = Object.create(MessageComposer.prototype);
            composer.props = {
                store: {capabilities: {scheduled_messages: true}},
                conversation: {conversation_type: "direct", can_send: false},
            };
            assert.notOk(
                composer.canCreateScheduledMessage,
                "receive-only conversations cannot create an external send"
            );
            composer.props.conversation.can_send = true;
            assert.ok(composer.canCreateScheduledMessage);
        }
    );

    QUnit.test(
        "schedule mode makes Enter and the primary action schedule-only",
        async (assert) => {
            const composer = Object.create(MessageComposer.prototype);
            const calls = {scheduled: 0, sent: 0};
            composer.props = {
                store: {
                    capabilities: {scheduled_messages: true},
                    isSending: () => false,
                },
                state: {replyTo: false},
                conversation: {
                    channel_id: 42,
                    conversation_type: "direct",
                    can_send: true,
                    capabilities: {},
                },
            };
            composer.local = {
                actionBusy: false,
                attachments: [],
                body: "Retorno combinado",
                mode: "message",
                recordingPhase: "idle",
                scheduledAt: localDateTimeInputValue(
                    new Date(Date.now() + 30 * 60 * 1000)
                ),
                tool: "schedule",
            };
            composer.scheduleCurrentMessage = async () => {
                calls.scheduled += 1;
                return true;
            };
            composer.send = async () => {
                calls.sent += 1;
                return true;
            };
            let prevented = 0;

            composer.onKeydown({
                key: "Enter",
                shiftKey: false,
                isComposing: false,
                ctrlKey: false,
                metaKey: false,
                preventDefault: () => {
                    prevented += 1;
                },
            });
            await Promise.resolve();
            assert.strictEqual(prevented, 1);
            assert.deepEqual(calls, {scheduled: 1, sent: 0});

            await composer.runPrimaryAction();
            assert.deepEqual(
                calls,
                {scheduled: 2, sent: 0},
                "the blue primary action never falls through to immediate send"
            );
            assert.ok(composer.scheduledTextOnlyAllowed);
            assert.ok(composer.canScheduleCurrentMessage);
            assert.notOk(
                composer.canAttach,
                "attachments stay disabled while scheduling"
            );

            composer.local.attachments = [{kind: "image"}];
            assert.notOk(
                composer.scheduledTextOnlyAllowed,
                "a late attachment invalidates the schedule intent"
            );
            assert.notOk(composer.canScheduleCurrentMessage);
        }
    );

    QUnit.test("persists only text and mode per database and user", (assert) => {
        const records = new Map();
        const storage = {
            getItem: (key) => records.get(key) || null,
            setItem: (key, value) => records.set(key, value),
            removeItem: (key) => records.delete(key),
        };
        const key = composerDraftStorageKey("odoo16_teste", 7);
        const drafts = new Map([
            [
                42,
                {
                    body: "Nota reservada",
                    mode: "note",
                    attachments: [{name: "contrato.pdf", mediaRef: "upload-secret"}],
                    replyTo: {message_id: 99},
                },
            ],
        ]);

        assert.ok(writeComposerDrafts(storage, key, drafts));
        const serialized = records.get(key);
        assert.notOk(serialized.includes("contrato.pdf"));
        assert.notOk(serialized.includes("upload-secret"));
        assert.notOk(serialized.includes("message_id"));
        assert.deepEqual(readComposerDrafts(storage, key).get(42), {
            body: "Nota reservada",
            mode: "note",
        });
        assert.notStrictEqual(
            composerDraftStorageKey("outra_base", 7),
            key,
            "database boundaries produce distinct keys"
        );
        assert.notStrictEqual(
            composerDraftStorageKey("odoo16_teste", 8),
            key,
            "user boundaries produce distinct keys"
        );
    });

    QUnit.test("persists complete recent drafts within a bounded quota", (assert) => {
        const records = new Map();
        const storage = {
            getItem: (key) => records.get(key) || null,
            setItem: (key, value) => records.set(key, value),
            removeItem: (key) => records.delete(key),
        };
        const key = composerDraftStorageKey("odoo16_teste", 7);
        const drafts = new Map();
        for (let channelId = 1; channelId <= 10; channelId += 1) {
            drafts.set(channelId, {
                body: String(channelId).repeat(65536),
                mode: "message",
            });
        }

        assert.ok(writeComposerDrafts(storage, key, drafts));
        const restored = readComposerDrafts(storage, key);
        assert.strictEqual(
            restored.get(10).body.length,
            65536,
            "the newest draft is never clipped below the outbound contract"
        );
        assert.notOk(restored.has(1), "old drafts yield before storage exceeds quota");
        assert.ok(
            restored.size < drafts.size,
            "the aggregate storage budget is enforced"
        );
    });

    QUnit.test("preserves draft recency independently of numeric IDs", (assert) => {
        const records = new Map();
        const storage = {
            getItem: (key) => records.get(key) || null,
            setItem: (key, value) => records.set(key, value),
            removeItem: (key) => records.delete(key),
        };
        const key = composerDraftStorageKey("odoo16_teste", 7);
        const drafts = new Map([
            [100, {body: "Primeiro", mode: "message"}],
            [2, {body: "Segundo", mode: "message"}],
            [50, {body: "Mais recente", mode: "note"}],
        ]);

        assert.ok(writeComposerDrafts(storage, key, drafts));
        assert.deepEqual(
            [...readComposerDrafts(storage, key).keys()],
            [100, 2, 50],
            "JSON round-trip cannot reorder numeric channel identifiers"
        );
    });

    QUnit.test("uses modifier-only shortcuts without capturing typing", (assert) => {
        assert.strictEqual(composerShortcut({key: "q"}), false);
        assert.strictEqual(
            composerShortcut({key: "q", ctrlKey: true, shiftKey: true}),
            "quick_reply"
        );
        assert.strictEqual(
            composerShortcut({key: "N", metaKey: true, shiftKey: true}),
            "note"
        );
        assert.strictEqual(
            composerShortcut({
                key: "N",
                metaKey: true,
                shiftKey: true,
                target: {tagName: "TEXTAREA", isContentEditable: false},
            }),
            false,
            "composer shortcuts do not take over a text field"
        );
        assert.strictEqual(
            composerShortcut({
                key: "f",
                ctrlKey: true,
                shiftKey: true,
                isComposing: true,
            }),
            false,
            "IME composition is never intercepted"
        );
        assert.strictEqual(conversationListShortcut({key: "/"}), "search");
        assert.strictEqual(
            conversationListShortcut({
                key: "/",
                target: {tagName: "INPUT", isContentEditable: false},
            }),
            false,
            "the list never captures shortcuts while typing"
        );
        assert.strictEqual(
            conversationListShortcut({key: "ArrowDown", altKey: true}),
            "next"
        );
        assert.strictEqual(
            conversationListShortcut({key: "ArrowUp", altKey: true}),
            "previous"
        );
    });

    QUnit.test(
        "keeps internal notes available on receive-only conversations",
        (assert) => {
            assert.notOk(
                conversationComposerAvailable({show_composer: false}, {}),
                "a fully read-only conversation keeps the informational footer"
            );
            assert.ok(
                conversationComposerAvailable(
                    {show_composer: false},
                    {internal_notes: true}
                ),
                "the note composer remains available when external sending is blocked"
            );
        }
    );

    QUnit.test(
        "uses provider-neutral contracts for notes and scheduling",
        async (assert) => {
            const calls = [];
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {
                capabilities: {internal_notes: true, scheduled_messages: true},
            };
            store.state.selectedChannelId = 42;
            store.call = (method, args) => {
                calls.push({method, args});
                return Promise.resolve({
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 42,
                    client_request_id:
                        method === "schedule_message" ? args[3] : args[2],
                    message:
                        method === "post_internal_note" ? {message_id: 17} : undefined,
                    scheduled_message:
                        method === "schedule_message" ? {id: 18} : undefined,
                });
            };
            store.refreshLatestTimeline = async () => true;
            store.refreshLoadedConversations = async () => true;
            store.loadProductivity = async () => true;

            assert.ok(await store.postInternalNote("  Somente para a equipe  "));
            const scheduledAt = localDateTimeInputValue(
                new Date(Date.now() + 30 * 60 * 1000)
            );
            assert.ok(
                await store.scheduleMessage("  Retorno combinado  ", scheduledAt)
            );

            assert.strictEqual(calls[0].method, "post_internal_note");
            assert.deepEqual(calls[0].args.slice(0, 2), [42, "Somente para a equipe"]);
            assert.ok(
                /^[0-9a-f-]{36}$/.test(calls[0].args[2]),
                "the note has a stable idempotency key"
            );
            assert.strictEqual(calls[1].method, "schedule_message");
            assert.deepEqual(calls[1].args.slice(0, 3), [
                42,
                "Retorno combinado",
                localDateTimeToOdooUtc(scheduledAt),
            ]);
            assert.ok(calls[1].args[2].endsWith("Z"));
            assert.ok(
                /^[0-9a-f-]{36}$/.test(calls[1].args[3]),
                "the scheduled message has a stable idempotency key"
            );
        }
    );

    QUnit.test(
        "reuses follow-up request UUIDs across ambiguous retries",
        async (assert) => {
            const calls = [];
            const attempts = new Map();
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {capabilities: {followups: true}};
            store.state.selectedChannelId = 42;
            store.loadProductivity = async () => true;
            store.refreshLoadedConversations = async () => true;
            store.call = async (method, args) => {
                calls.push({method, args});
                const count = (attempts.get(method) || 0) + 1;
                attempts.set(method, count);
                if (count === 1) {
                    throw new Error("Resposta perdida depois do commit");
                }
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 42,
                    client_request_id:
                        method === "schedule_followup"
                            ? args[1].client_request_id
                            : args[3],
                    activity: {id: 91},
                };
            };

            const values = {
                summary: "Retornar orçamento",
                note: "Cliente pediu retorno",
                date_deadline: "2030-01-02",
                user_id: 7,
            };
            assert.notOk(await store.scheduleFollowup(values));
            assert.ok(await store.scheduleFollowup(values));
            const scheduledCalls = calls.filter(
                (item) => item.method === "schedule_followup"
            );
            assert.strictEqual(scheduledCalls.length, 2);
            assert.strictEqual(
                scheduledCalls[0].args[1].client_request_id,
                scheduledCalls[1].args[1].client_request_id,
                "the retry replays the same schedule intent"
            );
            assert.ok(
                /^[0-9a-f-]{36}$/.test(scheduledCalls[0].args[1].client_request_id)
            );
            assert.notOk(
                Object.prototype.hasOwnProperty.call(values, "client_request_id"),
                "the caller payload is not mutated"
            );

            assert.notOk(await store.completeFollowup(91, "  Resolvido  "));
            assert.ok(await store.completeFollowup(91, "  Resolvido  "));
            const completionCalls = calls.filter(
                (item) => item.method === "complete_followup"
            );
            assert.deepEqual(completionCalls[0].args.slice(0, 3), [
                42,
                91,
                "Resolvido",
            ]);
            assert.strictEqual(
                completionCalls[0].args[3],
                completionCalls[1].args[3],
                "the retry replays the same completion intent"
            );
            assert.ok(/^[0-9a-f-]{36}$/.test(completionCalls[0].args[3]));
        }
    );

    QUnit.test(
        "keeps productivity lazy and refreshes an opened projection",
        async (assert) => {
            const calls = [];
            const response = {
                schema_version: SUPPORTED_SCHEMA_VERSION,
                channel_id: 42,
                activities: [],
                scheduled_messages: [],
                activity_types: [],
                assignable_users: [],
            };
            const store = new ContactCenterStore({
                orm: {},
                busService: {},
                notification: false,
            });
            store.state.bootstrap = {capabilities: {followups: true}};
            store.state.selectedChannelId = 42;
            store.resetProductivity(42);
            const synchronizations = [];
            store.scheduleSynchronization = (...args) => synchronizations.push(args);
            store.call = (method, args) => {
                calls.push({method, args});
                return Promise.resolve(response);
            };

            assert.ok(await store.loadProductivity());
            assert.ok(await store.loadProductivity());
            assert.strictEqual(calls.length, 1, "a ready projection remains lazy");
            assert.ok(
                store.handleProductivityNotification({
                    event_type: "productivity_updated",
                    channel_id: 42,
                })
            );
            await Promise.resolve();
            assert.strictEqual(
                calls.length,
                2,
                "an opened projection reacts to the bus"
            );
            assert.deepEqual(calls[0], {method: "get_productivity", args: [42]});
            assert.deepEqual(
                synchronizations,
                [[false, false]],
                "the conversation list also refreshes for follow-up filters and badges"
            );
        }
    );

    QUnit.test(
        "persists an opaque multi-intent idempotency journal across reloads",
        async (assert) => {
            const records = new Map();
            const storage = {
                getItem: (key) => records.get(key) || null,
                setItem: (key, value) => records.set(key, value),
                removeItem: (key) => records.delete(key),
            };
            let uuidSequence = 1;
            const operationCrypto = {
                randomUUID() {
                    const tail = String(uuidSequence).padStart(12, "0");
                    uuidSequence += 1;
                    return `00000000-0000-4000-8000-${tail}`;
                },
            };
            const makeStore = () => {
                const store = new ContactCenterStore({
                    orm: {},
                    busService: {},
                    notification: false,
                    operationStorage: storage,
                    operationDatabase: "odoo16_teste",
                    operationCrypto,
                });
                store.state.bootstrap = {user: {id: 7}, capabilities: {}};
                store.state.conversations = [
                    {channel_id: 10, conversation_type: "direct", can_send: true},
                ];
                store.state.selectedChannelId = 10;
                store.refreshLoadedConversations = async () => true;
                return store;
            };
            const attempts = [];
            const firstStore = makeStore();
            firstStore.call = async (_method, args) => {
                attempts.push(args);
                throw new Error("Resposta perdida depois do commit");
            };

            assert.notOk(await firstStore.sendMessage("Intenção A reservada"));
            assert.notOk(await firstStore.sendMessage("Intenção B reservada"));
            const firstARequestId = attempts[0][3];
            const firstBRequestId = attempts[1][3];
            assert.notStrictEqual(firstARequestId, firstBRequestId);

            const key = operationJournalStorageKey("odoo16_teste", 7);
            const serialized = records.get(key);
            assert.notOk(serialized.includes("Intenção A reservada"));
            assert.notOk(serialized.includes("Intenção B reservada"));
            assert.strictEqual(readOperationJournal(storage, key).size, 2);
            assert.strictEqual(
                operationIntentFingerprint("same"),
                operationIntentFingerprint("same")
            );
            assert.notStrictEqual(
                operationIntentFingerprint("same"),
                operationIntentFingerprint("different")
            );

            const secondStore = makeStore();
            secondStore.call = async (_method, args) => {
                attempts.push(args);
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 10,
                    client_request_id: args[3],
                    message: {message_id: 101},
                };
            };
            assert.ok(await secondStore.sendMessage("Intenção A reservada"));
            assert.strictEqual(
                attempts[2][3],
                firstARequestId,
                "A→B→reload→A recovers A instead of the most recent channel intent"
            );
            assert.strictEqual(
                readOperationJournal(storage, key).size,
                1,
                "only the strictly confirmed intent is removed"
            );

            const thirdStore = makeStore();
            thirdStore.call = async (_method, args) => {
                attempts.push(args);
                throw new Error("Nova tentativa ambígua");
            };
            assert.notOk(await thirdStore.sendMessage("Intenção A reservada"));
            assert.notStrictEqual(
                attempts[3][3],
                firstARequestId,
                "a confirmed operation receives a fresh UUID when intentionally repeated"
            );
        }
    );

    QUnit.test(
        "keeps the journal on invalid responses and replays expired schedule time",
        async (assert) => {
            const records = new Map();
            const storage = {
                getItem: (key) => records.get(key) || null,
                setItem: (key, value) => records.set(key, value),
                removeItem: (key) => records.delete(key),
            };
            let now = Date.parse("2030-01-01T12:00:00.000Z");
            let uuidSequence = 10;
            const operationCrypto = {
                randomUUID() {
                    const tail = String(uuidSequence).padStart(12, "0");
                    uuidSequence += 1;
                    return `00000000-0000-4000-8000-${tail}`;
                },
            };
            const makeStore = () => {
                const store = new ContactCenterStore({
                    orm: {},
                    busService: {},
                    notification: false,
                    operationStorage: storage,
                    operationDatabase: "odoo16_teste",
                    operationNow: () => now,
                    operationCrypto,
                });
                store.state.bootstrap = {
                    user: {id: 7},
                    capabilities: {scheduled_messages: true},
                };
                store.state.selectedChannelId = 42;
                store.loadProductivity = async () => true;
                return store;
            };
            const scheduledAt = localDateTimeInputValue(new Date(now + 2 * 60 * 1000));
            let firstRequestId = "";
            const firstStore = makeStore();
            firstStore.call = async (_method, args) => {
                firstRequestId = args[3];
                throw new Error("Resposta perdida depois do commit");
            };
            assert.notOk(
                await firstStore.scheduleMessage("Retorno exato", scheduledAt)
            );

            now += 5 * 60 * 1000;
            let replayRequestId = "";
            const replayStore = makeStore();
            replayStore.call = async (_method, args) => {
                replayRequestId = args[3];
                return {
                    schema_version: SUPPORTED_SCHEMA_VERSION,
                    channel_id: 42,
                    client_request_id: args[3],
                    scheduled_message: {id: 51},
                };
            };
            assert.ok(
                await replayStore.scheduleMessage("Retorno exato", scheduledAt),
                "a mutable client clock cannot block replay of an existing intent"
            );
            assert.strictEqual(replayRequestId, firstRequestId);

            assert.throws(
                () =>
                    validateOperationEnvelope(
                        {
                            schema_version: SUPPORTED_SCHEMA_VERSION,
                            channel_id: 99,
                            client_request_id: firstRequestId,
                            message: {message_id: 1},
                        },
                        {
                            channelId: 42,
                            requestId: firstRequestId,
                            requiredRecords: ["message"],
                        }
                    ),
                /outra conversa/,
                "a response for another conversation fails closed"
            );
        }
    );
});

QUnit.module("contact_center_ui > multi-file and structured messages", () => {
    const capabilities = {
        media: {
            image: {enabled: true, caption: true},
            audio: {enabled: true, caption: false},
        },
        outbound_structured_content: {
            buttons: {
                body_mode: "required",
                max_body_length: 1024,
                max_buttons: 3,
                action_types: ["reply", "url", "phone"],
                max_title_length: 60,
                max_footer_length: 60,
                max_button_title_length: 20,
                max_id_length: 200,
                max_phone_length: 30,
            },
            list: {
                body_mode: "required",
                max_body_length: 1024,
                max_sections: 10,
                max_rows: 10,
                max_title_length: 60,
                max_footer_length: 60,
                max_button_text_length: 20,
                max_section_title_length: 60,
                max_row_title_length: 24,
                max_row_description_length: 72,
                max_id_length: 200,
            },
            contacts: {
                body_mode: "none",
                max_body_length: 0,
                max_contacts: 1,
                max_name_length: 120,
                max_phones: 5,
                max_emails: 5,
                max_phone_length: 30,
                max_email_length: 254,
            },
            location: {
                body_mode: "none",
                max_body_length: 0,
                max_name_length: 120,
                max_address_length: 300,
                allow_live: false,
            },
        },
    };

    function attachment(id, kind = "image") {
        return {
            id,
            kind,
            name: `${id}.jpg`,
            mediaRef: `media-${id}`,
            phase: "ready",
            file: null,
            previewUrl: "",
        };
    }

    function makeComposer(items = [], body = "") {
        const store = new ContactCenterStore({
            orm: {},
            busService: {},
            notification: false,
            operationStorage: false,
        });
        const conversation = {
            channel_id: 10,
            conversation_type: "direct",
            state: "open",
            can_send: true,
            capabilities,
        };
        store.state.conversations = [conversation];
        store.state.selectedChannelId = 10;
        store.refreshLoadedConversations = async () => true;
        const composer = Object.create(MessageComposer.prototype);
        composer.props = {store, state: store.state, conversation};
        composer.local = {
            body,
            mode: "message",
            tool: false,
            attachments: items,
            sendPlan: false,
            structuredDraft: false,
            actionBusy: false,
            recordingPhase: "idle",
            uploadError: "",
        };
        composer.activeChannelId = 10;
        composer.destroyed = false;
        composer.uploadGeneration = 0;
        composer.uploadControllers = new Map();
        composer.drafts = new Map();
        composer.persistedDrafts = new Map();
        composer.fileRef = {el: null};
        composer.persistDraft = () => true;
        composer.resize = () => true;
        composer.revokePreview = () => true;
        composer.previewUrl = () => "";
        return composer;
    }

    function documentComposer() {
        const composer = makeComposer([attachment("existing")], "Confira a proposta");
        composer.conversation.identity = {id: 7, partner: {id: 30, company: {id: 31}}};
        composer.conversation.capabilities = {
            ...capabilities,
            media: {...capabilities.media, document: {enabled: true}},
        };
        composer.store.registerDraftAttachmentHandler((source) =>
            composer.addDraftAttachment(source)
        );
        composer.store.uploadMedia = async () => ({
            media_ref: "private-copy",
            media: {
                kind: "document",
                state: "ready",
                name: "S001.pdf",
                mimetype: "application/pdf",
                size_bytes: 10,
            },
        });
        return composer;
    }

    const documentSource = {
        channelId: 10,
        customerKey: "30:31",
        url: "/contact_center/customer/10/sale/1/pdf",
        name: "S001.pdf",
        mimetype: "application/pdf",
    };
    function documentResponse() {
        return {
            ok: true,
            blob: async () => new Blob(["%PDF-1.4"], {type: "application/pdf"}),
        };
    }

    QUnit.test(
        "customer document prepares a private copy in the existing draft without sending",
        async (assert) => {
            const composer = documentComposer();
            const originalFetch = window.fetch;
            const upload = composer.store.uploadMedia;
            composer.store.uploadMedia = async (file, _id, channelId) => {
                assert.ok(file instanceof File);
                assert.strictEqual(file.name, "S001.pdf");
                assert.strictEqual(channelId, 10);
                return upload();
            };
            window.fetch = async (url, options) => {
                assert.strictEqual(url, documentSource.url);
                assert.strictEqual(options.redirect, "error");
                assert.strictEqual(options.credentials, "same-origin");
                assert.ok(composer.local.actionBusy);
                return documentResponse();
            };
            try {
                assert.ok(await composer.store.addDraftAttachment(documentSource));
                assert.strictEqual(composer.local.body, "Confira a proposta");
                assert.strictEqual(composer.attachments[0].id, "existing");
                assert.strictEqual(composer.attachments[1].mediaRef, "private-copy");
                assert.strictEqual(composer.attachments[1].file, null);
                assert.notOk(composer.local.actionBusy);
                assert.deepEqual(composer.state.messages, []);
            } finally {
                window.fetch = originalFetch;
            }
        }
    );

    QUnit.test(
        "document bridge rejects notes, stale customers, external URLs and busy drafts before fetching",
        async (assert) => {
            const originalFetch = window.fetch;
            let fetches = 0;
            window.fetch = async () => {
                fetches += 1;
                return documentResponse();
            };
            try {
                for (const mutate of [
                    (composer) => {
                        composer.local.mode = "note";
                    },
                    (composer) => {
                        composer.local.actionBusy = true;
                    },
                    (composer) => {
                        composer.conversation.identity.partner.id = 99;
                    },
                    (composer) => {
                        composer.local.recordingPhase = "recording";
                    },
                ]) {
                    const composer = documentComposer();
                    mutate(composer);
                    assert.notOk(
                        await composer.store.addDraftAttachment(documentSource)
                    );
                    assert.strictEqual(composer.attachments.length, 1);
                    assert.strictEqual(composer.local.body, "Confira a proposta");
                }
                const composer = documentComposer();
                assert.notOk(
                    await composer.store.addDraftAttachment({
                        ...documentSource,
                        url: "https://example.com/file.pdf",
                    })
                );
                assert.notOk(
                    await composer.store.addDraftAttachment({
                        ...documentSource,
                        url: "/contact_center/../../web/login",
                    })
                );
                assert.strictEqual(fetches, 0);
                const unregisterOld = composer.store.registerDraftAttachmentHandler(
                    async () => false
                );
                composer.store.registerDraftAttachmentHandler(async () => true);
                unregisterOld();
                assert.ok(
                    await composer.store.addDraftAttachment(documentSource),
                    "old component cannot unregister its replacement"
                );
            } finally {
                window.fetch = originalFetch;
            }
        }
    );

    QUnit.test(
        "customer change while downloading discards the document and releases the composer",
        async (assert) => {
            const composer = documentComposer();
            const originalFetch = window.fetch;
            let resolve = null;
            window.fetch = () =>
                new Promise((done) => {
                    resolve = done;
                });
            try {
                const pending = composer.store.addDraftAttachment(documentSource);
                composer.conversation.identity.partner.id = 99;
                resolve(documentResponse());
                assert.notOk(await pending);
                assert.strictEqual(composer.attachments.length, 1);
                assert.notOk(composer.local.actionBusy);
                assert.strictEqual(composer.uploadControllers.size, 0);
            } finally {
                window.fetch = originalFetch;
            }
        }
    );

    QUnit.test(
        "mode change aborts document download without leaving the composer locked",
        async (assert) => {
            const composer = documentComposer();
            const originalFetch = window.fetch;
            window.fetch = (_url, options) =>
                new Promise((_resolve, reject) => {
                    options.signal.addEventListener("abort", () =>
                        reject(new DOMException("Aborted", "AbortError"))
                    );
                });
            try {
                const pending = composer.store.addDraftAttachment(documentSource);
                composer.local.mode = "note";
                composer.clearLocalAttachments();
                assert.notOk(await pending);
                assert.notOk(composer.local.actionBusy);
                assert.strictEqual(composer.local.mode, "note");
                assert.strictEqual(composer.local.body, "Confira a proposta");
            } finally {
                window.fetch = originalFetch;
            }
        }
    );

    QUnit.test(
        "failed document upload stays retryable and never reports success",
        async (assert) => {
            const composer = documentComposer();
            const originalFetch = window.fetch;
            window.fetch = async () => documentResponse();
            composer.store.uploadMedia = async () => {
                throw new Error("Upload denied");
            };
            try {
                assert.notOk(await composer.store.addDraftAttachment(documentSource));
                assert.strictEqual(composer.attachments[1].phase, "error");
                assert.strictEqual(composer.attachments[1].error, "Upload denied");
                assert.ok(composer.attachments[1].file instanceof File);
                assert.strictEqual(composer.attachments[0].id, "existing");
                assert.notOk(composer.local.actionBusy);
            } finally {
                window.fetch = originalFetch;
            }
        }
    );

    QUnit.test(
        "forced switch during document upload cleans only its original draft",
        async (assert) => {
            const composer = documentComposer();
            const originalFetch = window.fetch;
            window.fetch = async () => documentResponse();
            let resolveUpload = null;
            let uploadStarted = null;
            const started = new Promise((done) => {
                uploadStarted = done;
            });
            composer.store.uploadMedia = () =>
                new Promise((done) => {
                    resolveUpload = done;
                    uploadStarted();
                });
            try {
                const pending = composer.store.addDraftAttachment(documentSource);
                await started;
                composer.saveCurrentDraft();
                composer.state.selectedChannelId = 20;
                composer.activeChannelId = 20;
                composer.local.attachments = [attachment("other-draft")];
                composer.local.body = "Rascunho B";
                composer.local.actionBusy = true;
                resolveUpload({
                    media_ref: "late",
                    media: {kind: "document", state: "ready"},
                });
                assert.notOk(await pending);
                assert.deepEqual(
                    composer.attachments.map((item) => item.id),
                    ["other-draft"]
                );
                assert.strictEqual(composer.local.body, "Rascunho B");
                assert.ok(composer.local.actionBusy);
                assert.deepEqual(
                    composer.drafts.get(10).attachments.map((item) => item.id),
                    ["existing"]
                );
            } finally {
                window.fetch = originalFetch;
            }
        }
    );

    function accepted(args, id = 1) {
        return {
            schema_version: SUPPORTED_SCHEMA_VERSION,
            channel_id: args[0],
            client_request_id: args[3],
            message: {message_id: id, body_text: args[1]},
        };
    }

    QUnit.test(
        "caption belongs to the first compatible file and audio preserves separate text",
        (assert) => {
            const plan = buildAttachmentSendPlan(
                " Legenda única ",
                [attachment("a", "audio"), attachment("b"), attachment("c")],
                capabilities,
                7
            );
            assert.deepEqual(
                plan.map((item) => item.body),
                ["", "Legenda única", ""]
            );
            assert.deepEqual(
                plan.map((item) => item.replyId),
                [7, false, false]
            );
            const audioPlan = buildAttachmentSendPlan(
                "Texto preservado",
                [attachment("a", "audio"), attachment("b", "audio")],
                capabilities,
                7
            );
            assert.deepEqual(
                audioPlan.map((item) => [item.body, item.mediaRef]),
                [
                    ["Texto preservado", ""],
                    ["", "media-a"],
                    ["", "media-b"],
                ]
            );
            assert.deepEqual(
                audioPlan.map((item) => item.replyId),
                [7, false, false]
            );
            assert.strictEqual(
                buildAttachmentSendPlan("Texto", [], capabilities).length,
                1
            );
        }
    );

    QUnit.test(
        "partial timeout retries only pending files with the original request UUID",
        async (assert) => {
            const composer = makeComposer(
                [attachment("one"), attachment("two"), attachment("three")],
                "Uma legenda"
            );
            const calls = [];
            composer.store.call = async (_method, args) => {
                calls.push(args);
                if (calls.length === 2) {
                    throw new Error("HTTP timeout after uncertain admission");
                }
                return accepted(args, calls.length);
            };
            await composer.send();
            assert.deepEqual(
                composer.attachments.map((item) => item.id),
                ["two", "three"]
            );
            assert.strictEqual(
                composer.local.body,
                "",
                "the admitted caption is cleared exactly once"
            );
            assert.strictEqual(composer.local.sendPlan.length, 2);
            assert.ok(composer.local.sendStatus.includes("2 item"));
            assert.notOk(
                composer.canChooseAttachments,
                "an uncertain request cannot be edited into another intent"
            );
            composer.onInput({target: {value: "altered uncertain caption"}});
            assert.strictEqual(composer.local.body, "");
            await composer.send();
            assert.deepEqual(
                calls.map((args) => args[4][0]),
                ["media-one", "media-two", "media-two", "media-three"]
            );
            assert.strictEqual(
                calls[1][3],
                calls[2][3],
                "the uncertain item retains its UUID"
            );
            assert.notStrictEqual(calls[0][3], calls[1][3]);
            assert.deepEqual(
                calls.map((args) => args[1]),
                ["Uma legenda", "", "", ""]
            );
            assert.deepEqual(composer.attachments, []);
            assert.notOk(composer.local.sendPlan);
        }
    );

    QUnit.test(
        "an acknowledged send survives a failed conversation refresh",
        async (assert) => {
            const composer = makeComposer([attachment("one"), attachment("two")]);
            const calls = [];
            composer.store.call = async (_method, args) => {
                calls.push(args);
                return accepted(args, calls.length);
            };
            composer.store.refreshLoadedConversations = async () => {
                throw new Error("refresh unavailable");
            };
            await composer.send();
            assert.strictEqual(calls.length, 2);
            assert.deepEqual(composer.attachments, []);
            assert.notOk(
                composer.local.sendPlan,
                "view refresh cannot turn an acknowledged file into a pending send"
            );
        }
    );

    QUnit.test(
        "destroy stops the sequence after the in-flight acknowledgement",
        async (assert) => {
            const plan = buildAttachmentSendPlan(
                "",
                [attachment("one"), attachment("two")],
                capabilities
            );
            let active = true;
            let resolve = null;
            const admitted = [];
            let calls = 0;
            const sending = admitAttachmentSequence(plan, {
                isActive: () => active,
                send: () => {
                    calls += 1;
                    return new Promise((done) => {
                        resolve = done;
                    });
                },
                onAdmitted: (item) => admitted.push(item.attachmentId),
            });
            active = false;
            resolve(true);
            const result = await sending;
            assert.strictEqual(calls, 1);
            assert.deepEqual(admitted, ["one"]);
            assert.deepEqual(
                plan.map((item) => item.attachmentId),
                ["two"]
            );
            assert.notOk(result.complete);
        }
    );

    QUnit.test(
        "an indirect switch updates only the original draft after acknowledgement",
        async (assert) => {
            const composer = makeComposer(
                [attachment("one"), attachment("two")],
                "Legenda A"
            );
            let resolve = null;
            let callArgs = null;
            composer.store.call = (_method, args) => {
                callArgs = args;
                return new Promise((done) => {
                    resolve = done;
                });
            };
            const sending = composer.send();
            composer.saveCurrentDraft();
            composer.activeChannelId = 20;
            composer.state.selectedChannelId = 20;
            composer.local = {
                body: "Rascunho B",
                mode: "message",
                attachments: [],
                sendPlan: false,
            };
            resolve(accepted(callArgs));
            await sending;
            assert.strictEqual(composer.local.body, "Rascunho B");
            assert.deepEqual(composer.local.attachments, []);
            const draft = composer.drafts.get(10);
            assert.strictEqual(draft.body, "");
            assert.deepEqual(
                draft.attachments.map((item) => item.id),
                ["two"]
            );
            assert.deepEqual(
                draft.sendPlan.map((item) => item.attachmentId),
                ["two"]
            );
            assert.deepEqual(composer.state.messages, [], "A never appears in B");
        }
    );

    QUnit.test(
        "upload limit is atomic and retry retains each individual upload UUID",
        async (assert) => {
            const composer = makeComposer([attachment("existing")]);
            const file = {name: "picture.jpg", size: 42, type: "image/jpeg"};
            assert.notOk(
                await composer.prepareFilesUpload(
                    Array(MAX_COMPOSER_ATTACHMENTS).fill(file)
                )
            );
            assert.deepEqual(
                composer.attachments.map((item) => item.id),
                ["existing"]
            );
            assert.ok(composer.local.uploadError.includes("10"));
            const calls = [];
            composer.store.uploadMedia = async (_file, id) => {
                calls.push(id);
                if (calls.length === 1) {
                    throw new Error("network interrupted");
                }
                return {
                    media_ref: "prepared",
                    media: {
                        kind: "image",
                        state: "ready",
                        name: "picture.jpg",
                        size_bytes: 42,
                    },
                };
            };
            await composer.prepareFilesUpload([file]);
            const item = composer.attachments[1];
            assert.strictEqual(item.phase, "error");
            await composer.retryUpload(item);
            assert.deepEqual(calls, [item.id, item.id]);
            assert.strictEqual(item.phase, "ready");
            assert.strictEqual(item.file, null);
            assert.strictEqual(composer.attachments[0].id, "existing");
        }
    );

    QUnit.test(
        "removing or destroying an upload discards late results and aborts its controller",
        async (assert) => {
            for (const destroy of [false, true]) {
                const composer = makeComposer();
                const item = {
                    ...attachment("pending"),
                    file: {name: "photo.jpg"},
                    phase: "pending",
                    mediaRef: "",
                };
                composer.local.attachments = [item];
                let resolve = null;
                let signal = null;
                composer.store.uploadMedia = (_file, _id, _channel, requestSignal) => {
                    signal = requestSignal;
                    return new Promise((done) => {
                        resolve = done;
                    });
                };
                const uploading = composer.uploadPendingFile(item, 0, 10);
                if (destroy) {
                    composer.destroyed = true;
                    composer.clearLocalAttachments();
                } else {
                    composer.removeAttachment(item);
                }
                resolve({media_ref: "late", media: {kind: "image", state: "ready"}});
                await uploading;
                assert.ok(signal.aborted);
                assert.deepEqual(composer.attachments, []);
                assert.strictEqual(
                    item.mediaRef,
                    "",
                    "a stale response never resurrects an attachment"
                );
            }
        }
    );

    QUnit.test(
        "structured forms enforce body, boundaries and stable opaque option IDs",
        (assert) => {
            const buttons = newStructuredDraft("buttons", capabilities);
            buttons.rows[0].title = "Falar com vendas";
            const first = structuredDraftSubmission(
                buttons,
                "Como podemos ajudar?",
                capabilities
            );
            assert.strictEqual(first.content.buttons[0].id, buttons.rows[0].id);
            assert.deepEqual(
                first,
                structuredDraftSubmission(buttons, "Como podemos ajudar?", capabilities)
            );
            assert.notOk(structuredDraftSubmission(buttons, "", capabilities).content);
            assert.notOk(
                structuredDraftSubmission(buttons, "a".repeat(1025), capabilities)
                    .content
            );
            const list = newStructuredDraft("list", capabilities);
            list.rows = Array.from({length: 10}, (_, index) => ({
                id: String(index),
                title: `Opção ${index}`,
                description: "",
            }));
            assert.strictEqual(
                structuredDraftSubmission(list, "Escolha", capabilities).content
                    .sections[0].rows.length,
                10
            );
            list.rows.push({id: "overflow", title: "11", description: ""});
            assert.notOk(
                structuredDraftSubmission(list, "Escolha", capabilities).content
            );
            const contact = newStructuredDraft("contacts", capabilities);
            contact.name = "Maria";
            contact.phones = "+55 11 99999-0000";
            assert.ok(structuredDraftSubmission(contact, "", capabilities).content);
            assert.notOk(
                structuredDraftSubmission(contact, "não perder", capabilities).content
            );
            const location = newStructuredDraft("location", capabilities);
            location.latitude = "0";
            location.longitude = "0";
            assert.deepEqual(
                structuredDraftSubmission(location, "", capabilities).content,
                {
                    type: "location",
                    latitude: 0,
                    longitude: 0,
                    name: "",
                    address: "",
                }
            );
            location.latitude = "91";
            assert.notOk(structuredDraftSubmission(location, "", capabilities).content);
        }
    );

    QUnit.test(
        "structured sends preserve card and UUID across timeout and honor capabilities",
        async (assert) => {
            const composer = makeComposer();
            Object.defineProperty(composer, "voiceRecordingAvailable", {value: true});
            assert.ok(
                composer.showVoiceRecordingButton,
                "an empty composer offers voice recording"
            );
            composer.local.structuredDraft = newStructuredDraft(
                "contacts",
                capabilities
            );
            composer.local.structuredDraft.name = "Maria";
            composer.local.structuredDraft.phones = "+55 11 99999-0000";
            assert.notOk(
                composer.showVoiceRecordingButton,
                "a contact card exposes Send rather than a disabled microphone"
            );
            assert.ok(
                composer.primaryActionAvailable,
                "a contact card is sendable without text"
            );
            composer.local.structuredDraft = newStructuredDraft(
                "location",
                capabilities
            );
            composer.local.structuredDraft.latitude = "-23.5";
            composer.local.structuredDraft.longitude = "-46.6";
            assert.notOk(
                composer.showVoiceRecordingButton,
                "a location card also exposes Send"
            );
            assert.ok(
                composer.primaryActionAvailable,
                "coordinates are sendable without text"
            );
            const calls = [];
            composer.store.call = async (_method, args) => {
                calls.push(args);
                if (calls.length === 1) {
                    throw new Error("HTTP timeout");
                }
                return accepted(args);
            };
            await composer.send();
            assert.ok(composer.local.structuredDraft);
            assert.notOk(composer.canChooseStructured);
            assert.notOk(
                composer.showVoiceRecordingButton,
                "the pending card keeps its retry button"
            );
            assert.ok(
                composer.primaryActionAvailable,
                "retry remains available after the timeout"
            );
            const pendingDraft = composer.local.structuredDraft;
            composer.local.structuredDraft = false;
            assert.notOk(
                composer.showVoiceRecordingButton,
                "the pending plan alone also suppresses voice recording"
            );
            composer.local.structuredDraft = pendingDraft;
            await composer.send();
            assert.strictEqual(calls[0][3], calls[1][3]);
            assert.deepEqual(calls[0][5], calls[1][5]);
            assert.strictEqual(calls[0][1], "");
            assert.notOk(composer.local.structuredDraft);
            assert.ok(
                composer.showVoiceRecordingButton,
                "voice recording returns only after the card is acknowledged"
            );
            composer.props.conversation = {
                ...composer.props.conversation,
                capabilities: {...capabilities, outbound_structured_content: {}},
            };
            composer.state.conversations = [composer.props.conversation];
            assert.notOk(
                await composer.store.sendMessage("", [], {
                    structuredContent: calls[0][5],
                })
            );
            assert.notOk(
                await composer.store.sendMessage("", ["media-one"], {
                    structuredContent: calls[0][5],
                })
            );
            assert.strictEqual(calls.length, 2);
        }
    );

    QUnit.test(
        "a definitive card rejection unlocks editing without losing its fields",
        async (assert) => {
            const composer = makeComposer();
            composer.local.structuredDraft = newStructuredDraft(
                "location",
                capabilities
            );
            composer.local.structuredDraft.latitude = "0";
            composer.local.structuredDraft.longitude = "20";
            const calls = [];
            composer.store.call = async (_method, args) => {
                calls.push(args);
                if (calls.length === 1) {
                    throw {
                        data: {
                            name: "odoo.exceptions.UserError",
                            message: "Provider rejects this coordinate",
                        },
                    };
                }
                return accepted(args);
            };
            await composer.send();
            assert.notOk(composer.local.sendPlan);
            assert.ok(
                composer.canChooseStructured,
                "a rolled-back RPC permits correction"
            );
            assert.strictEqual(composer.local.structuredDraft.longitude, "20");
            composer.local.structuredDraft.latitude = "1";
            await composer.send();
            assert.strictEqual(calls.length, 2);
            assert.notStrictEqual(
                calls[0][3],
                calls[1][3],
                "the corrected content owns a new intent"
            );
            assert.strictEqual(calls[1][5].latitude, 1);
            assert.notOk(composer.local.structuredDraft);
        }
    );

    QUnit.test(
        "a rejection after a timeout cannot dismiss an earlier uncertain admission",
        async (assert) => {
            const composer = makeComposer([attachment("one")], "Texto");
            const calls = [];
            composer.store.call = async (_method, args) => {
                calls.push(args);
                if (calls.length === 1) {
                    throw new Error("timeout");
                }
                throw {
                    data: {
                        name: "odoo.exceptions.AccessError",
                        message: "Access revoked after first request",
                    },
                };
            };
            await composer.send();
            await composer.send();
            assert.ok(composer.local.sendPlan);
            assert.strictEqual(calls[0][3], calls[1][3]);
            assert.notOk(composer.canChooseAttachments);
            assert.strictEqual(composer.local.body, "Texto");
        }
    );

    QUnit.test(
        "a frozen reply can retry after its target leaves the loaded page",
        async (assert) => {
            const composer = makeComposer([], "Resposta");
            composer.state.replyTo = {message_id: 7};
            composer.state.messages = [{message_id: 7, actions: {reply: true}}];
            const calls = [];
            composer.store.call = async (_method, args) => {
                calls.push(args);
                if (calls.length === 1) {
                    throw new Error("timeout");
                }
                return accepted(args, 8);
            };
            await composer.send();
            composer.state.messages = [];
            await composer.send();
            assert.strictEqual(calls.length, 2);
            assert.strictEqual(calls[0][3], calls[1][3]);
            assert.strictEqual(
                calls[1][2],
                7,
                "the server revalidates the captured target in its conversation"
            );
            assert.notOk(composer.local.sendPlan);
        }
    );

    QUnit.test(
        "a pre-dispatch reply refusal releases the unsent plan for correction",
        async (assert) => {
            const composer = makeComposer([], "Resposta ainda local");
            composer.state.replyTo = {message_id: 7};
            composer.state.messages = [{message_id: 7, actions: {reply: false}}];
            composer.store.call = async () =>
                assert.ok(false, "no request was dispatched");
            await composer.send();
            assert.notOk(composer.local.sendPlan);
            assert.strictEqual(composer.local.body, "Resposta ainda local");
            composer.onInput({target: {value: "Corrigida"}});
            assert.strictEqual(composer.local.body, "Corrigida");
        }
    );

    QUnit.test(
        "outbound capability specs fail closed without legacy or partial defaults",
        async (assert) => {
            const valid = capabilities.outbound_structured_content;
            assert.deepEqual(outboundStructuredCapabilities(capabilities), valid);
            const missingActionTypes = {...valid.buttons};
            delete missingActionTypes.action_types;
            const malformed = [
                undefined,
                [],
                ["buttons"],
                {buttons: true},
                {buttons: missingActionTypes},
                {buttons: {...valid.buttons, action_types: []}},
                {buttons: {...valid.buttons, action_types: ["reply", "reply"]}},
                {buttons: {...valid.buttons, action_types: ["script"]}},
                {buttons: {...valid.buttons, max_buttons: true}},
                {buttons: {...valid.buttons, max_buttons: 26}},
                {buttons: {...valid.buttons, max_id_length: 0}},
                {buttons: {...valid.buttons, max_body_length: 65537}},
                {buttons: {...valid.buttons, body_mode: "none"}},
                {buttons: {...valid.buttons, provider_default: true}},
                {location: {...valid.location, allow_live: "false"}},
                {...valid, unknown: {}},
                {...valid, location: {...valid.location, max_address_length: -1}},
            ];
            const composer = makeComposer();
            composer.store.call = async () =>
                assert.ok(false, "malformed capabilities must not dispatch an RPC");
            for (const map of malformed) {
                const rejected = {outbound_structured_content: map};
                assert.deepEqual(outboundStructuredCapabilities(rejected), {});
                assert.notOk(newStructuredDraft("buttons", rejected));
                composer.props.conversation = {
                    ...composer.props.conversation,
                    capabilities: rejected,
                };
                composer.state.conversations = [composer.props.conversation];
                assert.notOk(
                    await composer.store.sendMessage("Mensagem", [], {
                        structuredContent: {type: "buttons"},
                    })
                );
            }
            assert.deepEqual(
                outboundStructuredCapabilities({structured_content: ["buttons"]}),
                {},
                "the former list never enables a form"
            );
            assert.notOk(newStructuredDraft("toString", capabilities));
            assert.notOk(
                structuredDraftSubmission({type: "toString"}, "", capabilities).content
            );
        }
    );

    QUnit.test(
        "reply-only channel specs control action choices and preserve a now unsupported draft",
        (assert) => {
            const composer = makeComposer([], "Mensagem");
            composer.onStructuredType({target: {value: "buttons"}});
            const draft = composer.local.structuredDraft;
            draft.rows[0].type = "url";
            draft.rows[0].title = "Visitar";
            draft.rows[0].url = "https://example.com";
            assert.ok(
                composer.structuredSubmission.content,
                "the original channel supports URL buttons"
            );
            composer.saveCurrentDraft();
            composer.local.structuredDraft = false;
            const replyOnly = {
                outbound_structured_content: {
                    buttons: {
                        ...capabilities.outbound_structured_content.buttons,
                        action_types: ["reply"],
                        max_buttons: 1,
                        max_button_title_length: 10,
                        max_body_length: 40,
                    },
                },
            };
            composer.props.conversation = {
                ...composer.props.conversation,
                capabilities: replyOnly,
            };
            composer.restoreDraft(10);
            assert.deepEqual(composer.structuredTypes, ["buttons"]);
            assert.deepEqual(composer.structuredActions, ["reply"]);
            assert.strictEqual(composer.structuredRowLimit, 1);
            assert.strictEqual(composer.structuredRowTitleLimit, 10);
            assert.strictEqual(
                composer.bodyMaxLength,
                80,
                "the HTML guard allows astral characters; validation still enforces 40 code points"
            );
            assert.strictEqual(
                composer.local.structuredDraft.rows[0].type,
                "url",
                "restoring never converts an unsupported action"
            );
            assert.strictEqual(
                composer.local.structuredDraft.rows[0].url,
                "https://example.com"
            );
            assert.ok(composer.structuredSubmission.error.includes("ação"));
            assert.notOk(composer.primaryActionAvailable);
            composer.onStructuredType({target: {value: "contacts"}});
            assert.strictEqual(
                composer.local.structuredDraft.type,
                "buttons",
                "an unsupported forged choice cannot discard the draft"
            );
            composer.onStructuredType({target: {value: "buttons"}});
            assert.strictEqual(composer.local.structuredDraft.rows[0].type, "reply");
            composer.local.structuredDraft.rows[0].title = "Escolher";
            assert.ok(composer.primaryActionAvailable);
            assert.notOk(composer.canAddStructuredRow);
            composer.addStructuredRow();
            assert.strictEqual(composer.local.structuredDraft.rows.length, 1);
            composer.local.body =
                "Mensagem que excede o limite de quarenta caracteres deste canal";
            assert.notOk(composer.primaryActionAvailable);
            assert.ok(
                composer.local.body.length > 40,
                "a stricter channel never truncates existing text"
            );
        }
    );

    QUnit.test(
        "larger channel specs and optional bodies replace fixed provider limits",
        (assert) => {
            const base = capabilities.outbound_structured_content;
            const expanded = {
                outbound_structured_content: {
                    buttons: {
                        ...base.buttons,
                        body_mode: "optional",
                        max_body_length: 2048,
                        action_types: ["phone", "url"],
                        max_buttons: 5,
                        max_title_length: 200,
                        max_footer_length: 120,
                        max_button_title_length: 40,
                        max_phone_length: 50,
                    },
                    list: {
                        ...base.list,
                        max_rows: 12,
                        max_row_title_length: 36,
                        max_row_description_length: 100,
                    },
                    contacts: {
                        ...base.contacts,
                        body_mode: "optional",
                        max_body_length: 80,
                        max_name_length: 300,
                        max_phones: 6,
                        max_phone_length: 45,
                    },
                    location: {
                        ...base.location,
                        body_mode: "required",
                        max_body_length: 80,
                        max_name_length: 200,
                        max_address_length: 800,
                    },
                },
            };
            const composer = makeComposer();
            composer.props.conversation = {
                ...composer.props.conversation,
                capabilities: expanded,
            };
            composer.onStructuredType({target: {value: "buttons"}});
            assert.deepEqual(composer.structuredActions, ["phone", "url"]);
            assert.strictEqual(
                composer.local.structuredDraft.rows[0].type,
                "phone",
                "new rows start with the first allowed action"
            );
            while (composer.canAddStructuredRow) {
                composer.addStructuredRow();
            }
            assert.strictEqual(composer.local.structuredDraft.rows.length, 5);
            for (const row of composer.local.structuredDraft.rows) {
                assert.strictEqual(row.type, "phone");
                row.title = "Título de botão maior que vinte";
                row.phone = "1".repeat(40);
            }
            composer.local.structuredDraft.title = "T".repeat(100);
            assert.ok(
                composer.primaryActionAvailable,
                "this profile permits five long buttons with no body"
            );
            composer.local.body = "a".repeat(1500);
            assert.ok(
                composer.primaryActionAvailable,
                "the former 1024-character cap does not leak into this profile"
            );
            assert.strictEqual(composer.bodyMaxLength, 4096);
            const list = newStructuredDraft("list", expanded);
            list.rows = Array.from({length: 12}, (_, index) => ({
                id: String(index),
                title: "T".repeat(30),
                description: "D".repeat(90),
            }));
            assert.ok(structuredDraftSubmission(list, "Escolha", expanded).content);
            assert.notOk(
                structuredDraftSubmission(list, "Escolha", capabilities).content
            );
            const contact = newStructuredDraft("contacts", expanded);
            contact.name = "N".repeat(180);
            contact.phones = Array(6).fill("1".repeat(40)).join("\n");
            assert.ok(
                structuredDraftSubmission(contact, "Observação opcional", expanded)
                    .content
            );
            const location = newStructuredDraft("location", expanded);
            location.latitude = "0";
            location.longitude = "0";
            location.name = "N".repeat(160);
            location.address = "E".repeat(600);
            assert.notOk(structuredDraftSubmission(location, "", expanded).content);
            assert.ok(
                structuredDraftSubmission(location, "Descrição obrigatória", expanded)
                    .content
            );
        }
    );

    QUnit.test(
        "zero optional limits and compact option IDs remain explicit without rewriting drafts",
        (assert) => {
            const restricted = {
                outbound_structured_content: {
                    list: {
                        ...capabilities.outbound_structured_content.list,
                        max_rows: 40,
                        max_id_length: 1,
                        max_title_length: 0,
                        max_footer_length: 0,
                        max_row_description_length: 0,
                    },
                    contacts: {
                        ...capabilities.outbound_structured_content.contacts,
                        max_phones: 0,
                        max_emails: 0,
                    },
                },
            };
            const list = newStructuredDraft("list", restricted);
            for (let index = 1; index < 40; index++) {
                list.rows.push(newStructuredRow("reply", 1, list.rows));
            }
            for (const row of list.rows) {
                row.title = "Opção";
            }
            assert.strictEqual(new Set(list.rows.map((row) => row.id)).size, 40);
            assert.ok(list.rows.every((row) => row.id.length === 1));
            assert.ok(structuredDraftSubmission(list, "Escolha", restricted).content);
            list.rows[0].description = "Descrição preservada";
            assert.notOk(
                structuredDraftSubmission(list, "Escolha", restricted).content
            );
            assert.strictEqual(list.rows[0].description, "Descrição preservada");
            const contact = newStructuredDraft("contacts", restricted);
            contact.name = "Ana";
            assert.ok(structuredDraftSubmission(contact, "", restricted).content);
            contact.phones = "11999990000";
            assert.notOk(structuredDraftSubmission(contact, "", restricted).content);
            const composer = makeComposer();
            composer.local.structuredDraft = contact;
            composer.props.conversation = {
                ...composer.props.conversation,
                capabilities: {},
            };
            assert.deepEqual(composer.structuredTypes, []);
            assert.ok(
                composer.hasProductivityToolbar,
                "the unavailable card can still be explicitly removed"
            );
            assert.notOk(composer.primaryActionAvailable);
            composer.onStructuredType({target: {value: ""}});
            assert.notOk(composer.local.structuredDraft);
        }
    );

    QUnit.test(
        "uncertain cards confirm the original UUID after capabilities shrink or disappear",
        async (assert) => {
            for (const removeType of [false, true]) {
                const composer = makeComposer(
                    [],
                    "Mensagem já enviada antes da alteração do canal"
                );
                composer.onStructuredType({target: {value: "buttons"}});
                composer.local.structuredDraft.rows[0].type = "url";
                composer.local.structuredDraft.rows[0].title = "Abrir o site";
                composer.local.structuredDraft.rows[0].url = "https://example.com";
                const calls = [];
                composer.store.call = async (_method, args) => {
                    calls.push(args);
                    if (calls.length === 1) {
                        throw new Error("Timeout after possible admission");
                    }
                    return accepted(args);
                };
                await composer.send();
                assert.ok(composer.canReplayPendingSend);
                assert.strictEqual(composer.local.sendPlan[0].requestId, calls[0][3]);
                const reduced = {
                    outbound_structured_content: removeType
                        ? {}
                        : {
                              buttons: {
                                  ...capabilities.outbound_structured_content.buttons,
                                  action_types: ["reply"],
                                  max_body_length: 4,
                                  max_button_title_length: 5,
                              },
                          },
                };
                composer.props.conversation = {
                    ...composer.props.conversation,
                    capabilities: reduced,
                };
                composer.state.conversations = [composer.props.conversation];
                assert.notOk(
                    composer.structuredSubmission.content,
                    "the old card is no longer a valid new admission"
                );
                assert.ok(
                    composer.primaryActionAvailable,
                    "confirmation of the uncertain admission remains available"
                );
                assert.notOk(
                    composer.canChooseStructured,
                    "the pending card remains immutable"
                );
                composer.store.operationJournal.clear();
                await composer.send();
                assert.deepEqual(
                    calls[1],
                    calls[0],
                    "the original content and UUID survive even journal eviction"
                );
                assert.notOk(composer.local.sendPlan);
                assert.notOk(composer.local.structuredDraft);
                composer.local.body = calls[0][1];
                composer.local.structuredDraft = newStructuredDraft(
                    "buttons",
                    capabilities
                );
                composer.local.structuredDraft.rows[0].title = "Novo botão";
                assert.notOk(
                    composer.primaryActionAvailable,
                    "new sends still obey the current channel spec"
                );
                await composer.send();
                assert.strictEqual(calls.length, 2);
            }
        }
    );

    QUnit.test(
        "card text limits count Unicode code points and preserve valid astral characters",
        (assert) => {
            const buttons = newStructuredDraft("buttons", capabilities);
            buttons.rows[0].title = "😀".repeat(20);
            buttons.title = "😀".repeat(60);
            buttons.footer = "😀".repeat(60);
            assert.ok(
                structuredDraftSubmission(buttons, "😀".repeat(1024), capabilities)
                    .content
            );
            buttons.rows[0].title += "😀";
            assert.notOk(
                structuredDraftSubmission(buttons, "Texto", capabilities).content,
                "21 code points exceed the 20-character button limit"
            );
            buttons.rows[0].title = "😀".repeat(20);
            assert.notOk(
                structuredDraftSubmission(buttons, "😀".repeat(1025), capabilities)
                    .content
            );
            const list = newStructuredDraft("list", capabilities);
            list.rows[0].title = "😀".repeat(24);
            list.rows[0].description = "😀".repeat(72);
            assert.ok(structuredDraftSubmission(list, "Escolha", capabilities).content);
            list.rows[0].description += "😀";
            assert.notOk(
                structuredDraftSubmission(list, "Escolha", capabilities).content
            );
            const location = newStructuredDraft("location", capabilities);
            location.latitude = "1";
            location.longitude = "2";
            location.name = "😀".repeat(120);
            location.address = "😀".repeat(300);
            assert.ok(structuredDraftSubmission(location, "", capabilities).content);
            location.name += "😀";
            assert.notOk(structuredDraftSubmission(location, "", capabilities).content);
            const composer = makeComposer([], "😀".repeat(1024));
            composer.local.structuredDraft = buttons;
            assert.strictEqual(
                composer.bodyMaxLength,
                2048,
                "HTML's UTF-16 maxlength cannot cut a valid code-point body"
            );
            assert.ok(composer.primaryActionAvailable);
        }
    );

    QUnit.test(
        "received cards never execute opaque selection IDs or unsafe links",
        (assert) => {
            assert.strictEqual(
                safeStructuredUrl("https://example.com/path"),
                "https://example.com/path"
            );
            for (const value of [
                "javascript:alert(1)",
                "data:text/html,unsafe",
                "https://user:secret@example.com",
                "https://localhost/path",
                "https://127.0.0.1/path",
                "https://example.com/?token=secret",
                "https://example.com:8080/path",
            ]) {
                assert.strictEqual(safeStructuredUrl(value), "", value);
            }
            const selection = structuredMessageCard({
                type: "selection",
                id: "javascript:alert(1)",
                title: "Vendas",
            });
            assert.strictEqual(selection.title, "Vendas");
            assert.notOk(selection.href);
            const shared = structuredMessageCard({
                type: "shared",
                items: [{kind: "story", url: "javascript:alert(1)", title: "Story"}],
            });
            assert.strictEqual(shared.items[0].href, "");
            assert.notOk(
                structuredMessageCard({type: "location", latitude: 200, longitude: 0})
                    .href
            );
            assert.ok(
                structuredMessageCard({
                    type: "location",
                    latitude: 0,
                    longitude: 0,
                }).href.includes("mlat=0")
            );
        }
    );
});
