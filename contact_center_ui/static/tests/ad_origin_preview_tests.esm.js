/** @odoo-module **/

/* global QUnit */

import {
    AdOriginPreview,
    MessageAdOriginPreviews,
    adOriginPreviewsForMessage,
} from "@contact_center_ui/js/ad_origin_preview.esm";
import {click, getFixture, mount, nextTick} from "@web/../tests/helpers/utils";
import {
    normalizeAdOriginPreview,
    normalizeAttributionProjection,
    safeAdOriginSourceUrl,
} from "@contact_center_ui/js/contact_center_model.esm";
import {AttributionTouchpoints} from "@contact_center_ui/js/attribution_touchpoints.esm";
import {MessageContent} from "@contact_center_ui/js/message_content.esm";
import {dialogService} from "@web/core/dialog/dialog_service";
import {hotkeyService} from "@web/core/hotkeys/hotkey_service";
import {makeFakeLocalizationService} from "@web/../tests/helpers/mock_services";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";
import {ormService} from "@web/core/orm_service";
import {reactive} from "@odoo/owl";
import {registry} from "@web/core/registry";
import {uiService} from "@web/core/ui/ui_service";

const PREVIEW_REF = "0a0b0c0d-1111-4222-8333-444455556666";
const OTHER_REF = "1a0b0c0d-1111-4222-8333-444455556666";

function preview(overrides = {}) {
    return {
        public_ref: PREVIEW_REF,
        title: "Solicite um orçamento gratuito!",
        body: "Conheça nossos serviços industriais.",
        source_url: "https://www.instagram.com/p/ABC123/",
        thumbnail_url: `/contact_center/attribution/${PREVIEW_REF}/thumbnail`,
        media_type: "image",
        state: "ready",
        presentation_source: "provider_snapshot",
        observed_at: "2026-09-12 12:00:00",
        fetched_at: "2026-09-12 12:00:01",
        ...overrides,
    };
}

function message(overrides = {}) {
    return {
        message_id: 84,
        channel_id: 10,
        content_type: "text",
        body_text: "Hello Can I get more info",
        direction: "inbound",
        media: [],
        actions: {reply: true},
        ad_origin_previews: [preview()],
        ...overrides,
    };
}

function attribution(overrides = {}) {
    return {
        public_ref: OTHER_REF,
        touchpoint_type: "paid_ad_click",
        evidence_level: "provider_asserted",
        network: "meta",
        source_platform: "instagram",
        source_type: "ad",
        entry_point_source: "ctwa_ad",
        entry_point_app: "instagram",
        utm_source: "instagram",
        utm_medium: "paid_social",
        utm_campaign: "Campanha de serviços",
        creative_media_type: "image",
        show_ad_attribution: true,
        occurred_at: "2026-09-12 12:00:00",
        ...overrides,
    };
}

QUnit.module("contact_center_ui ad origin preview", (hooks) => {
    let originalObserver = null;
    hooks.beforeEach(() => {
        registry.category("services").add("orm", ormService);
        registry.category("services").add("ui", uiService);
        registry.category("services").add("hotkey", hotkeyService);
        registry.category("services").add("dialog", dialogService);
        makeFakeLocalizationService();
        originalObserver = window.IntersectionObserver;
        // Keep synthetic thumbnails outside the loading viewport. These tests
        // exercise the card and its error callback without requesting a real image.
        window.IntersectionObserver = class {
            observe() {
                return undefined;
            }
            disconnect() {
                return undefined;
            }
        };
    });
    hooks.afterEach(() => {
        window.IntersectionObserver = originalObserver;
    });

    QUnit.test(
        "public links accept only authorized social hosts and public query keys",
        (assert) => {
            for (const url of [
                "https://instagram.com/p/ABC123/",
                "https://www.instagram.com/reel/ABC123/",
                "https://m.instagram.com/p/ABC123/",
                "https://facebook.com/story.php?story_fbid=123&id=456",
                "https://www.facebook.com/photo.php?fbid=123",
                "https://m.facebook.com/watch/?v=123",
                "https://fb.me/ABC123",
                "https://www.fb.me/ABC123",
                "https://facebook.com:443/photo.php?fbid=123",
                "https://instagram.com/directory/",
                "https://facebook.com/login-news/",
            ]) {
                assert.strictEqual(safeAdOriginSourceUrl(url), new URL(url).href, url);
            }
            for (const url of [
                // eslint-disable-next-line no-script-url -- test rejection, never navigation
                "javascript:alert(1)",
                "http://www.instagram.com/p/ABC123/",
                "//www.instagram.com/p/ABC123/",
                "https://instagram.com.evil.invalid/p/ABC123/",
                "https://evil.instagram.com/p/ABC123/",
                "https://user:password@instagram.com/p/ABC123/",
                "https://instagram.com:8443/p/ABC123/",
                "https://instagram.com/p/ABC123/#secret",
                "https://instagram.com/p/ABC123/#",
                "https://instagram.com/p/ABC123/?access_token=secret",
                "https://facebook.com/story.php?story_fbid=123&id=456&next=evil",
                "https://facebook.com/story.php?id=123&id=456",
                "https://facebook.com/story.php?id=",
                "https://facebook.com/story.php?id=%3Cscript%3E",
                "https://facebook.com/story.php?id=" + "a".repeat(257),
                "https://facebook.com\\@evil.invalid/",
                "https://face\nbook.com/",
                "https://127.0.0.1/",
                "https://www.instagram.com/p/" + "a".repeat(2050),
                "https://instagram.com/direct",
                "https://www.instagram.com/direct/inbox/",
                "https://m.instagram.com/accounts/login/",
                "https://instagram.com/%64irect/inbox/",
                "https://instagram.com/DIRECT/inbox/",
                "https://instagram.com//direct/inbox/",
                "https://instagram.com/%2Fdirect/inbox/",
                "https://facebook.com/messages/t/123",
                "https://www.facebook.com/login/",
                "https://m.facebook.com/settings",
                "https://facebook.com/dialog/share",
                "https://facebook.com/adsmanager/",
                "https://fb.me/messages/",
                "https://instagram.com/settings/",
                "https://facebook.com/direct/",
            ]) {
                assert.strictEqual(safeAdOriginSourceUrl(url), "", url);
            }
        }
    );

    QUnit.test(
        "preview normalization strips technical data and bounds text and thumbnails",
        (assert) => {
            const normalized = normalizeAdOriginPreview(
                preview({
                    title: "A".repeat(300),
                    body: "B".repeat(2500),
                    private_locator_ref: "secret",
                    thumbnail_url: "https://cdn.invalid/image?token=secret",
                    source_url: "https://facebook.com/?token=secret",
                })
            );
            assert.strictEqual(normalized.title.length, 256);
            assert.strictEqual(normalized.body.length, 2000);
            assert.strictEqual(normalized.thumbnail_url, "");
            assert.strictEqual(normalized.source_url, "");
            assert.notOk(JSON.stringify(normalized).includes("secret"));
            for (const thumbnail of [
                `/contact_center/attribution/${OTHER_REF}/thumbnail`,
                `/contact_center/attribution/${PREVIEW_REF}/thumbnail?token=secret`,
                "data:image/png;base64,private",
                `/web/image/${PREVIEW_REF}`,
            ]) {
                assert.strictEqual(
                    normalizeAdOriginPreview(preview({thumbnail_url: thumbnail}))
                        .thumbnail_url,
                    ""
                );
            }
            assert.notOk(normalizeAdOriginPreview(preview({public_ref: "invalid"})));
            assert.notOk(normalizeAdOriginPreview(preview({state: "unknown"})));
            assert.notOk(
                normalizeAdOriginPreview(preview({presentation_source: "unknown"}))
            );
            assert.strictEqual(
                normalizeAdOriginPreview(preview({media_type: "script"})).media_type,
                "unknown"
            );
            assert.strictEqual(
                normalizeAdOriginPreview(preview({fetched_at: "not a date"}))
                    .fetched_at,
                ""
            );
        }
    );

    QUnit.test(
        "message cards are deduplicated, bounded and absent from hidden deleted content",
        (assert) => {
            const item = message({ad_origin_previews: [preview(), preview(), null]});
            const before = JSON.stringify(item);
            assert.strictEqual(adOriginPreviewsForMessage(item).length, 1);
            assert.strictEqual(
                JSON.stringify(item),
                before,
                "the original message is untouched"
            );
            assert.deepEqual(
                adOriginPreviewsForMessage({...item, is_deleted: true}),
                []
            );
            assert.strictEqual(
                adOriginPreviewsForMessage({
                    ...item,
                    is_deleted: true,
                    deleted_content_visible: true,
                }).length,
                1
            );
            assert.deepEqual(
                adOriginPreviewsForMessage(message({ad_origin_previews: null})),
                []
            );
            assert.strictEqual(
                adOriginPreviewsForMessage(
                    message({
                        ad_origin_previews: Array.from({length: 12}, (_value, index) =>
                            preview({
                                public_ref: `${String(index).padStart(
                                    8,
                                    "0"
                                )}-1111-4222-8333-444455556666`,
                            })
                        ),
                    })
                ).length,
                8
            );
        }
    );

    QUnit.test(
        "attribution allow-list includes a valid preview and preserves older projections",
        (assert) => {
            const envelope = {
                enabled: true,
                items: [
                    attribution({
                        ad_origin_preview: preview({private_locator_ref: "secret"}),
                    }),
                ],
                has_more: false,
                next_cursor: false,
            };
            const normalized = normalizeAttributionProjection(envelope);
            assert.strictEqual(
                normalized.items[0].ad_origin_preview.title,
                preview().title
            );
            assert.notOk(JSON.stringify(normalized).includes("secret"));
            assert.deepEqual(
                normalizeAttributionProjection({...envelope, items: [attribution()]})
                    .items[0],
                attribution()
            );
            assert.notOk(
                normalizeAttributionProjection({
                    ...envelope,
                    items: [attribution({ad_origin_preview: {malformed: true}})],
                }).items[0].ad_origin_preview
            );
        }
    );

    QUnit.test(
        "complete card escapes text and reserves space for a private thumbnail",
        async (assert) => {
            const title = '<img src=x onerror="alert(1)">';
            const body = "<script>private()</script> Oferta";
            const env = await makeTestEnv();
            const target = getFixture();
            const component = await mount(AdOriginPreview, target, {
                env,
                props: {preview: preview({title, body})},
            });
            assert.strictEqual(
                target.querySelector(".cc-ad-origin-preview__title").textContent,
                title
            );
            assert.strictEqual(
                target.querySelector(".cc-ad-origin-preview__body").textContent,
                body
            );
            assert.containsNone(target, "script");
            assert.containsOnce(target, ".cc-ad-origin-preview__media img");
            const thumbnail = target.querySelector(".cc-ad-origin-preview__media img");
            assert.strictEqual(thumbnail.getAttribute("width"), "640");
            assert.strictEqual(thumbnail.getAttribute("height"), "360");
            assert.notOk(
                thumbnail.hasAttribute("src"),
                "lazy thumbnail does not load before visibility"
            );
            const link = target.querySelector(".cc-ad-origin-preview__link");
            assert.strictEqual(link.getAttribute("href"), preview().source_url);
            assert.strictEqual(link.getAttribute("target"), "_blank");
            assert.strictEqual(link.getAttribute("rel"), "noopener noreferrer");
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "partial and unavailable previews keep evidence without inventing creative content",
        async (assert) => {
            const env = await makeTestEnv();
            const target = getFixture();
            const component = await mount(AdOriginPreview, target, {
                env,
                props: {
                    preview: preview({
                        title: "",
                        body: "",
                        source_url: "",
                        thumbnail_url: "",
                        state: "unavailable",
                    }),
                },
            });
            assert.containsOnce(target, ".cc-ad-origin-preview");
            assert.containsNone(target, "img");
            assert.containsNone(target, "a");
            assert.containsNone(target, ".cc-ad-origin-preview__title");
            assert.ok(target.textContent.includes("Prévia do anúncio indisponível"));
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "pending thumbnail changes to ready without hiding the received title",
        async (assert) => {
            const item = reactive(preview({state: "pending"}));
            const env = await makeTestEnv();
            const target = getFixture();
            const component = await mount(AdOriginPreview, target, {
                env,
                props: {preview: item},
            });
            assert.ok(target.textContent.includes(item.title));
            assert.ok(target.textContent.includes("Carregando imagem do anúncio"));
            assert.containsNone(target, "img");
            item.state = "ready";
            await nextTick();
            assert.containsOnce(target, "img");
            assert.notOk(target.textContent.includes("Carregando imagem do anúncio"));
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "image failure preserves copy and link and cannot affect a newer preview",
        async (assert) => {
            const item = reactive(preview());
            const env = await makeTestEnv();
            const target = getFixture();
            const component = await mount(AdOriginPreview, target, {
                env,
                props: {preview: item},
            });
            const oldErrorHandler = component.imageErrorHandler;
            oldErrorHandler();
            await nextTick();
            assert.containsNone(target, "img");
            assert.ok(target.textContent.includes("Imagem indisponível"));
            assert.ok(target.textContent.includes(item.title));
            assert.containsOnce(target, ".cc-ad-origin-preview__link");
            item.fetched_at = "2026-09-12 12:05:00";
            await nextTick();
            assert.containsOnce(target, "img");
            oldErrorHandler();
            await nextTick();
            assert.containsOnce(
                target,
                "img",
                "late failure belongs only to the former thumbnail"
            );
            component.__owl__.app.destroy();
        }
    );

    QUnit.test("body expansion stays within the current preview", async (assert) => {
        const body = "Texto detalhado da oferta. ".repeat(30);
        const item = reactive(preview({body}));
        const env = await makeTestEnv();
        const target = getFixture();
        const component = await mount(AdOriginPreview, target, {
            env,
            props: {preview: item},
        });
        assert.ok(
            target.querySelector(".cc-ad-origin-preview__body").textContent.length <
                body.length
        );
        await click(target, ".cc-ad-origin-preview__expand");
        assert.strictEqual(
            target.querySelector(".cc-ad-origin-preview__body").textContent,
            body.trim()
        );
        assert.strictEqual(
            target
                .querySelector(".cc-ad-origin-preview__expand")
                .getAttribute("aria-expanded"),
            "true"
        );
        item.public_ref = OTHER_REF;
        await nextTick();
        assert.strictEqual(
            target
                .querySelector(".cc-ad-origin-preview__expand")
                .getAttribute("aria-expanded"),
            "false"
        );
        component.__owl__.app.destroy();
    });

    QUnit.test(
        "message integration renders the ad once after the original text",
        async (assert) => {
            const item = message({ad_origin_previews: [preview(), preview()]});
            const before = JSON.stringify(item);
            const env = await makeTestEnv();
            const target = getFixture();
            const component = await mount(MessageContent, target, {
                env,
                props: {message: item},
            });
            const original = target.querySelector(".cc-message-body > p");
            assert.strictEqual(original.textContent, "Hello Can I get more info");
            assert.containsOnce(target, ".cc-ad-origin-preview");
            assert.ok(
                original.nextElementSibling.classList.contains("cc-ad-origin-previews")
            );
            assert.strictEqual(
                JSON.stringify(item),
                before,
                "body and actions remain unchanged"
            );
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "redaction and conversation replacement remove previously visible preview data",
        async (assert) => {
            const item = reactive(message());
            const env = await makeTestEnv();
            const target = getFixture();
            const component = await mount(MessageAdOriginPreviews, target, {
                env,
                props: {message: item},
            });
            assert.containsOnce(target, ".cc-ad-origin-preview");
            item.is_deleted = true;
            await nextTick();
            assert.containsNone(target, ".cc-ad-origin-preview");
            item.deleted_content_visible = true;
            await nextTick();
            assert.containsOnce(target, ".cc-ad-origin-preview");
            item.ad_origin_previews = [];
            await nextTick();
            assert.containsNone(target, ".cc-ad-origin-preview");
            assert.notOk(target.textContent.includes(preview().title));
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "attribution panel reuses the card for a conversation-only touchpoint",
        async (assert) => {
            const projection = normalizeAttributionProjection({
                enabled: true,
                items: [attribution({ad_origin_preview: preview({thumbnail_url: ""})})],
                has_more: false,
                next_cursor: false,
            });
            const env = await makeTestEnv();
            const target = getFixture();
            const component = await mount(AttributionTouchpoints, target, {
                env,
                props: {attribution: {...projection, phase: "ready"}, store: {}},
            });
            assert.containsOnce(
                target,
                ".cc-attribution__timeline .cc-ad-origin-preview"
            );
            assert.ok(target.textContent.includes("Campanha de serviços"));
            assert.ok(target.textContent.includes(preview().title));
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "catalog preview is identified as later information and labels the real link host",
        async (assert) => {
            const env = await makeTestEnv();
            const target = getFixture();
            const component = await mount(AdOriginPreview, target, {
                env,
                props: {
                    preview: preview({
                        presentation_source: "marketing_catalog",
                        source_url: "https://facebook.com/instagram.com/example",
                        thumbnail_url: "",
                    }),
                },
            });
            assert.ok(target.textContent.includes("Prévia consultada posteriormente"));
            assert.containsOnce(target, ".cc-ad-origin-preview__provenance time");
            assert.ok(target.textContent.includes("Ver anúncio no Facebook"));
            assert.notOk(target.textContent.includes("marketing_catalog"));
            component.__owl__.app.destroy();
        }
    );
});
