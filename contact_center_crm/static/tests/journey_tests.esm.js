/** @odoo-module **/
/* global QUnit */
import "@contact_center_crm/js/journey.esm";
import {
    nextAnimationFrame,
    start,
    startServer,
} from "@mail/../tests/helpers/test_utils";
import {addFakeModel} from "@bus/../tests/helpers/model_definitions_helpers";
import {makeDeferred} from "@web/../tests/helpers/utils";
addFakeModel("crm.lead", {name: {string: "Business", type: "char"}});
QUnit.testDone(({module, name, failed}) => {
    if (module.startsWith("contact_center_crm > journey")) {
        console.log(`CC_JOURNEY_CASE ${JSON.stringify({name, failed})}`);
    }
});
const row = (id) => ({
    channel_id: id,
    can_open: true,
    inbox_name: "Comercial",
    state: "open",
    responsible: "Agente",
    scope: "context",
    writer: "automation",
    origins: {status: "ready", items: []},
});
async function setup(options = {}) {
    await startServer();
    const calls = [];
    const result = await start({
        mockRPC(route, args) {
            if (args.model === "crm.lead") {
                calls.push(args);
                if (args.method === "get_contact_center_journey") {
                    if (options.error) {
                        throw new Error("offline");
                    }
                    if (
                        args.kwargs.area === "linked" &&
                        options.errorOffset === args.kwargs.offset
                    ) {
                        options.errorOffset = null;
                        throw new Error("page offline once");
                    }
                    if (options.deferred) {
                        return options.deferred;
                    }
                    const offset = args.kwargs.offset || 0;
                    const items =
                        args.kwargs.area === "linked"
                            ? options.items || []
                            : options.contextItems || [];
                    return {
                        status: "ready",
                        items: items.slice(offset, offset + 20),
                        total: items.length,
                        offset,
                        limit: 20,
                        has_more: offset + 20 < items.length,
                    };
                }
                if (args.method === "get_contact_center_origin_page") {
                    return (
                        options.originPage || {
                            status: "ready",
                            items: [],
                            has_more: false,
                            next_offset: 40,
                        }
                    );
                }
                if (args.method === "open_contact_center_journey_conversation") {
                    return options.openDeferred;
                }
                if (
                    [
                        "action_website_journey_match",
                        "action_website_journey_matches",
                        "action_contact_center_scope",
                        "action_contact_center_origin_review",
                    ].includes(args.method)
                ) {
                    if (options.actionError) throw new Error("revoked");
                    return {
                        type: "ir.actions.act_window",
                        res_model: "crm.lead",
                        res_id: 41,
                        views: [[false, "form"]],
                    };
                }
                if (args.method === "get_contact_center_journey_history") {
                    return {status: "restricted", items: [], has_more: false};
                }
            }
            if (args.model === "contact.center.ui.api") {
                if (args.method === "systray_summary") {
                    return {schema_version: 1, enabled: false};
                }
                if (args.method === "get_connection_health") {
                    return {schema_version: 1, items: [], summary: {total: 0}};
                }
                if (args.method === "bootstrap") {
                    return {
                        schema_version: 1,
                        user: {id: 3},
                        accounts: [],
                        agents: [],
                        tags: [],
                        capabilities: {},
                    };
                }
            }
        },
    });
    await result.env.services.action.doAction({
        type: "ir.actions.client",
        tag: "contact_center_crm.journey",
        params: {lead_id: 41},
    });
    await nextAnimationFrame();
    return {...result, calls};
}
QUnit.module("contact_center_crm > journey", () => {
    QUnit.test(
        "minimal origins show labels and append a bounded page without opening technical evidence",
        async (assert) => {
            const item = row(91);
            item.scope = "confirmed";
            item.scope_decision_mode = "automatic_intake";
            item.origins = {
                status: "scope_review",
                has_more: true,
                next_offset: 20,
                items: [
                    {
                        type: "paid_ad_click",
                        at: "2026-10-08 10:00:00",
                        campaign_name: "Solar campaign",
                        ad_name: "Solar ad",
                        source_name: "Meta",
                        medium_name: "Paid",
                        scope: "pending",
                        review_reason: "after_closed",
                    },
                ],
            };
            const {click, calls} = await setup({
                items: [item],
                originPage: {
                    status: "ready",
                    has_more: false,
                    next_offset: 40,
                    items: [
                        {
                            type: "website",
                            campaign_name: "Search campaign",
                            scope: "ineligible",
                        },
                    ],
                },
            });
            assert.ok(document.body.textContent.includes("Solar campaign"));
            assert.ok(document.body.textContent.includes("Solar ad"));
            assert.ok(document.body.textContent.includes("após encerramento"));
            await click(".cc-journey-more-origins");
            assert.ok(document.body.textContent.includes("Search campaign"));
            assert.ok(
                document.body.textContent.includes("evidência permanece preservada")
            );
            assert.containsNone(document.body, ".cc-journey-more-origins");
            assert.notOk(
                calls.some((call) => call.method === "action_website_journey_matches")
            );
        }
    );
    QUnit.test(
        "reviewing one captured origin passes only its opaque key and refreshes after closing",
        async (assert) => {
            const item = row(91);
            const key = "a".repeat(64);
            item.origins.items = [
                {
                    type: "paid_ad_click",
                    evidence_key: key,
                    scope: "pending",
                    can_review: true,
                },
            ];
            const {click, calls, env} = await setup({items: [item]});
            let closed = null;
            env.services.action.doAction = async (_action, settings) => {
                closed = settings.onClose;
            };
            await click(".cc-journey-review-origin");
            const call = calls.find(
                (value) => value.method === "action_contact_center_origin_review"
            );
            assert.deepEqual(call.args, [[41], 91, key]);
            assert.strictEqual(typeof closed, "function");
            await closed();
            await nextAnimationFrame();
            assert.strictEqual(
                calls.filter(
                    (value) =>
                        value.method === "get_contact_center_journey" &&
                        value.kwargs.area === "linked"
                ).length,
                2
            );
        }
    );
    QUnit.test("intake writer is explicit and never confirms scope", async (assert) => {
        await setup({items: [{...row(91), writer: "intake"}]});
        assert.ok(document.body.textContent.includes("Entrada automática"));
        assert.ok(document.body.textContent.includes("Contexto sem crédito"));
        assert.notOk(document.body.textContent.includes("Legado indeterminado"));
    });
    QUnit.test(
        "empty business and customer context remain independently visible",
        async (assert) => {
            const {calls} = await setup();
            assert.containsN(document.body, ".cc-journey-empty", 2);
            assert.deepEqual(calls.map((c) => c.kwargs.area).sort(), [
                "context",
                "linked",
            ]);
        }
    );
    QUnit.test(
        "restricted rows have no chat action and history respects ACL",
        async (assert) => {
            const {click, calls} = await setup({
                items: [row(91), {can_open: false, restricted: true}],
            });
            assert.containsOnce(document.body, ".cc-journey-open");
            assert.containsOnce(document.body, ".cc-journey-restricted");
            assert.ok(document.body.textContent.includes("Contexto sem crédito"));
            await click(".cc-journey-history");
            assert.ok(document.body.textContent.includes("Histórico restrito"));
            assert.notOk(calls.some((c) => c.method === "get_timeline"));
        }
    );
    QUnit.test(
        "each area paginates without leaking restricted metadata",
        async (assert) => {
            const {click, calls} = await setup({
                items: Array.from({length: 21}, (_, i) => row(91 + i)),
            });
            await click(".cc-journey-next");
            assert.containsOnce(document.body, ".cc-journey-row");
            assert.strictEqual(calls[calls.length - 1].kwargs.offset, 20);
        }
    );
    QUnit.test(
        "customer context never renders private Website acquisition",
        async (assert) => {
            const own = row(91);
            own.origins.items = [
                {
                    type: "website_whatsapp",
                    reference: "OWN-WEBSITE",
                    privacy: "ready",
                    campaign: {status: "utm", name: "Own campaign"},
                },
            ];
            const context = row(92);
            context.origins = {
                status: "ready",
                website_has_more: true,
                items: [
                    {type: "conversation_start"},
                    {
                        type: "website_whatsapp",
                        reference: "PRIVATE-CONTEXT",
                        privacy: "ready",
                        campaign: {status: "utm", name: "Private campaign"},
                    },
                ],
            };
            await setup({items: [own], contextItems: [context]});
            const text = document.body.textContent;
            assert.ok(text.includes("OWN-WEBSITE"));
            assert.ok(text.includes("Own campaign"));
            assert.ok(text.includes("Início de conversa"));
            assert.notOk(text.includes("PRIVATE-CONTEXT"));
            assert.notOk(text.includes("Private campaign"));
            assert.containsN(document.body, ".cc-journey-row", 2);
            assert.containsOnce(document.body, ".cc-journey-row li button");
            assert.containsNone(document.body, ".cc-journey-more-origins");
        }
    );
    QUnit.test("failed listing exposes a retry", async (assert) => {
        await setup({error: true});
        assert.containsN(document.body, 'section [role="alert"]', 2);
    });
    QUnit.test("retry repeats the failed next page", async (assert) => {
        const {click, calls} = await setup({
            items: Array.from({length: 21}, (_, i) => row(91 + i)),
            errorOffset: 20,
        });
        await click(".cc-journey-next");
        assert.containsOnce(document.body, 'section [role="alert"]');
        assert.ok(document.querySelector(".cc-journey-next").disabled);
        await click('section [role="alert"] button');
        assert.containsOnce(document.body, ".cc-journey-row");
        assert.deepEqual(
            calls
                .filter(
                    (c) =>
                        c.method === "get_contact_center_journey" &&
                        c.kwargs.area === "linked"
                )
                .map((c) => c.kwargs.offset),
            [0, 20, 20]
        );
    });
    QUnit.test(
        "missing clocks and acquisition fallback remain factual",
        async (assert) => {
            const item = row(91);
            item.scope = "confirmed";
            item.origins.items = ["cookie", "none", "track"].map((provenance) => ({
                type: "website_whatsapp",
                match_state: "reference",
                scope: "claim_unavailable",
                privacy: "ready",
                campaign: {status: "restricted"},
                provenance,
                acquisition_at: provenance === "track" ? "2026-10-07 10:00:01" : false,
                at: "2026-10-07 10:00:00",
                clicked_at: "2026-10-07 10:00:00",
                page_url: "https://example.test/click",
                landing_url:
                    provenance === "track" ? "https://example.test/landing" : false,
            }));
            item.origins.marketing_restricted = true;
            await setup({items: [item]});
            const text = document.body.textContent;
            assert.ok(text.includes("Em aberto (fim exclusivo)"));
            assert.ok(text.includes("Mensagem: Não identificado"));
            assert.notOk(text.includes("Mensagem: Em aberto"));
            assert.ok(text.includes("Cookie de campanha"));
            assert.ok(text.includes("Origem não identificada"));
            assert.ok(text.includes("Histórico de visitas do site"));
            assert.strictEqual(
                (text.match(/a ocorrência usa o horário do clique/g) || []).length,
                3
            );
            assert.ok(text.includes("Página do clique: https://example.test/click"));
            assert.strictEqual(
                (text.match(/Página de aquisição: Não identificada\./g) || []).length,
                2
            );
            assert.ok(
                text.includes("Outras origens Marketing exigem permissão adicional")
            );
            assert.ok(text.includes("Campanha com acesso restrito"));
            assert.containsN(document.body, ".cc-journey-row li button", 3);
            assert.notOk(text.includes("Origens com acesso restrito."));
        }
    );
    QUnit.test(
        "a successful review clears an earlier evidence error",
        async (assert) => {
            const item = row(91);
            item.origins.items = [
                {
                    type: "website_whatsapp",
                    match_id: 12,
                    privacy: "ready",
                    campaign: {status: "unknown"},
                },
            ];
            const options = {items: [item], actionError: true};
            const {click, env, calls} = await setup(options);
            let closed = null;
            env.services.action.doAction = async (_action, settings) => {
                closed = settings && settings.onClose;
            };
            await click(".cc-journey-row li button");
            assert.ok(document.body.textContent.includes("Origem indisponível"));
            options.actionError = false;
            await click(".cc-journey-confirm");
            assert.notOk(document.body.textContent.includes("Origem indisponível"));
            assert.strictEqual(typeof closed, "function");
            await closed();
            await nextAnimationFrame();
            assert.strictEqual(
                calls.filter(
                    (c) =>
                        c.method === "get_contact_center_journey" &&
                        c.kwargs.area === "linked"
                ).length,
                2
            );
            assert.notOk(document.body.textContent.includes("Origem indisponível"));
        }
    );
    QUnit.test(
        "website chain keeps acquisition, click and message clocks with explicit scope",
        async (assert) => {
            const item = row(91);
            item.origins = {
                status: "ready",
                website_has_more: true,
                items: [
                    {
                        type: "website_whatsapp",
                        reference: "A2B3",
                        privacy: "ready",
                        scope: "outside_period",
                        match_id: 12,
                        campaign: {
                            status: "resolved",
                            name: "Synthetic campaign",
                            evidence: "url_and_local_catalog",
                        },
                        acquisition_at: "2026-10-06 09:00:00",
                        clicked_at: "2026-10-07 10:00:00",
                        message_at: "2026-10-07 10:01:00",
                        landing_url: "https://example.test/product",
                    },
                ],
            };
            const {click, calls, env} = await setup({items: [item]});
            const actions = [];
            env.services.action.doAction = async (action) => actions.push(action);
            const text = document.body.textContent;
            assert.ok(text.includes("Synthetic campaign"));
            assert.ok(text.includes("A2B3"));
            assert.ok(text.includes("Aquisição:"));
            assert.ok(text.includes("Mensagem:"));
            assert.ok(text.includes("fora do período"));
            assert.containsOnce(document.body, ".cc-journey-more-origins");
            await click(".cc-journey-more-origins");
            assert.ok(
                calls.some(
                    (call) =>
                        call.method === "get_contact_center_origin_page" &&
                        call.args[1] === 91 &&
                        call.args[2] === 20
                )
            );
            assert.strictEqual(actions.length, 0);
            assert.containsNone(document.body, ".cc-journey-more-origins");
            assert.notOk(document.body.textContent.includes("Não foi possível"));
        }
    );
    QUnit.test(
        "human association and catalog/privacy states stay distinct",
        async (assert) => {
            const item = row(91);
            item.origins.items = [
                {
                    scope: "eligible",
                    match_state: "confirmed",
                    privacy: "ready",
                    campaign: {status: "utm", name: "Informed"},
                },
                {
                    scope: "pending",
                    match_state: "suggested",
                    privacy: "decision_changed",
                    campaign: {status: "ambiguous", url_campaign_id: "123"},
                },
                {
                    scope: "scope_review",
                    match_state: "reference",
                    privacy: "capture_unavailable",
                    campaign: {status: "unresolved", url_campaign_id: "456"},
                },
                {
                    scope: "legacy_collision",
                    match_state: "rejected",
                    privacy: "legacy",
                    campaign: {status: "restricted"},
                },
            ].map((origin) => ({
                type: "website_whatsapp",
                reference: "A2B3",
                match_id: 12,
                ...origin,
            }));
            await setup({items: [item]});
            const text = document.body.textContent;
            for (const label of [
                "Referência do clique",
                "Associação confirmada pela equipe",
                "Código recebido na mensagem",
                "UTM informado: Informed",
                "mais de uma conta no catálogo",
                "não encontrada no catálogo",
                "revogada ou substituída",
                "Aguardando processamento",
                "Contexto: revisar",
                "Evento já capturado",
                "Campanha com acesso restrito",
                "Captura anterior",
            ])
                assert.ok(text.includes(label), label);
        }
    );
    QUnit.test("blocked and legacy origins never say processing", async (assert) => {
        const item = row(91);
        item.origins.items = ["legacy", "capture_unavailable", "decision_changed"].map(
            (scope) => ({
                type: "website_whatsapp",
                reference: "A2B3",
                match_id: 12,
                match_state: "reference",
                scope,
                privacy: scope,
                campaign: {status: "unknown"},
            })
        );
        await setup({items: [item]});
        const text = document.body.textContent;
        assert.ok(text.includes("Captura anterior à jornada: sem crédito automático"));
        assert.ok(text.includes("Captura bloqueada pela configuração atual"));
        assert.ok(
            text.includes("Decisão de consentimento alterada antes da atribuição")
        );
        assert.notOk(text.includes("Aguardando processamento"));
    });
    QUnit.test("evidence click passes a concrete authorized action", async (assert) => {
        const item = row(91);
        item.origins.items = [
            {
                type: "website_whatsapp",
                reference: "A2B3",
                match_state: "confirmed",
                privacy: "ready",
                scope: "eligible",
                match_id: 12,
                campaign: {status: "unknown"},
            },
        ];
        const {click, calls, env} = await setup({items: [item]});
        const actions = [];
        env.services.action.doAction = async (action) => actions.push(action);
        await click(".cc-journey-row li button");
        assert.deepEqual(
            calls.find((c) => c.method === "action_website_journey_match").args,
            [[41], 12]
        );
        assert.strictEqual(actions[0].res_model, "crm.lead");
        assert.notOk(document.body.textContent.includes("Não foi possível"));
    });
    QUnit.test("revoked evidence reports action failure", async (assert) => {
        const item = row(91);
        item.origins.items = [
            {
                type: "website_whatsapp",
                reference: "A2B3",
                privacy: "privacy_unavailable",
                match_id: 12,
                campaign: {status: "erased"},
            },
        ];
        const {click} = await setup({items: [item], actionError: true});
        await click(".cc-journey-row li button");
        assert.ok(
            document.body.textContent.includes(
                "Origem indisponível. Confira seu acesso."
            )
        );
    });
    QUnit.test(
        "erased website chain hides campaign and page without hiding its audit",
        async (assert) => {
            const item = row(91);
            item.origins.items = [
                {
                    type: "website_whatsapp",
                    reference: "A2B3",
                    privacy: "privacy_unavailable",
                    match_id: 12,
                    campaign: {status: "erased"},
                },
            ];
            await setup({items: [item]});
            assert.ok(document.body.textContent.includes("Apagado por privacidade"));
            assert.ok(document.body.textContent.includes("A2B3"));
            assert.notOk(document.body.textContent.includes("gclid"));
        }
    );
    QUnit.test(
        "removed visitor leaves a visible gap in the frozen business chain",
        async (assert) => {
            const item = row(91);
            item.origins.items = [
                {
                    type: "website_whatsapp",
                    reference: "FROZEN-ORIGIN",
                    visitor_state: "removed_or_unavailable",
                    privacy: "ready",
                    campaign: {status: "utm", name: "Frozen campaign"},
                    acquisition_at: "2026-10-06 10:00:00",
                    at: "2026-10-06 10:00:00",
                },
            ];
            await setup({items: [item]});
            const text = document.body.textContent;
            assert.ok(
                text.includes("Vínculo com o visitante removido ou indisponível.")
            );
            assert.ok(text.includes("FROZEN-ORIGIN"));
            assert.ok(text.includes("Frozen campaign"));
            assert.ok(text.includes("Aquisição:"));
            assert.notOk(text.includes("Apagado por privacidade"));
        }
    );
    QUnit.test("closing during listing ignores late results", async (assert) => {
        const deferred = makeDeferred();
        const {click} = await setup({deferred});
        assert.containsN(document.body, 'section [role="status"]', 2);
        await click(".o_dialog .modal-footer button");
        deferred.resolve({
            status: "ready",
            items: [],
            total: 0,
            offset: 0,
            limit: 20,
            has_more: false,
        });
        await nextAnimationFrame();
        assert.containsNone(document.body, ".cc-crm-journey");
    });
    QUnit.test(
        "double click admits one open and closing suppresses late chat",
        async (assert) => {
            const deferred = makeDeferred();
            const {click, calls} = await setup({
                items: [row(91)],
                openDeferred: deferred,
            });
            document.querySelector(".cc-journey-open").click();
            document.querySelector(".cc-journey-open").click();
            await nextAnimationFrame();
            assert.strictEqual(
                calls.filter(
                    (c) => c.method === "open_contact_center_journey_conversation"
                ).length,
                1
            );
            await click(".o_dialog .modal-footer button");
            deferred.resolve({schema_version: 1, item: {channel_id: 91}});
            await nextAnimationFrame();
            assert.containsNone(document.body, ".o_ChatWindow");
        }
    );
});
