/** @odoo-module **/

/* global QUnit */

import {
    AudioPlayer,
    FloatingVideo,
    downloadableMessageMedia,
    hasCompactAudio,
} from "@contact_center_ui/js/message_content.esm";
import {click, getFixture, mount, nextTick} from "@web/../tests/helpers/utils";
import {ContactCenterStore} from "@contact_center_ui/js/contact_center_store.esm";
import {ConversationTimeline} from "@contact_center_ui/js/conversation_timeline.esm";
import {makeFakeLocalizationService} from "@web/../tests/helpers/mock_services";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";
import {ormService} from "@web/core/orm_service";
import {reactive} from "@odoo/owl";
import {registry} from "@web/core/registry";
import {uiService} from "@web/core/ui/ui_service";

function readyMedia(overrides = {}) {
    return {
        id: 8,
        kind: "audio",
        state: "ready",
        name: "voz.ogg",
        duration_seconds: 8,
        is_voice_note: true,
        content_url: "/contact_center/media/8/content",
        download_url: "/contact_center/media/8/content?download=1",
        ...overrides,
    };
}

async function mountedTimeline(messages) {
    registry.category("services").add("ui", uiService);
    registry.category("services").add("orm", ormService);
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
        "position:fixed;left:120px;top:16px;width:560px;height:280px;overflow:hidden;z-index:10000";
    const store = new ContactCenterStore({
        orm: {},
        busService: new EventTarget(),
        stateFactory: reactive,
    });
    Object.assign(store.state, {
        bootstrap: {capabilities: {}},
        selectedChannelId: 10,
        timelineChannelId: 10,
        conversations: [{channel_id: 10, state: "resolved"}],
        timelinePhase: "ready",
        messages,
    });
    store.markSeen = async () => false;
    const timeline = await mount(ConversationTimeline, target, {
        env,
        props: {state: store.state, store},
    });
    timeline.viewportRef.el.parentElement.style.height = "280px";
    async function settle() {
        await nextTick();
        await new Promise((resolve) => requestAnimationFrame(resolve));
        await new Promise((resolve) => requestAnimationFrame(resolve));
        await nextTick();
    }
    return {
        target,
        store,
        timeline,
        settle,
        close: () => {
            timeline.__owl__.app.destroy();
            store.destroy();
            target.style.cssText = originalStyle;
        },
    };
}

function timelineMessage(overrides = {}) {
    return {
        message_id: 84,
        content_type: "media",
        body_text: "",
        date: "2026-09-09 12:00:00",
        direction: "outbound",
        origin: "external_device",
        platform: "whatsapp",
        provider: "wuzapi",
        author: {id: 12, type: "partner", name: "Agente"},
        actions: {},
        reactions: [],
        structured_content: {},
        media: [readyMedia()],
        ...overrides,
    };
}

QUnit.module("contact_center_ui > media presentation", () => {
    QUnit.test(
        "downloads accept only ready local media for their exact id",
        (assert) => {
            const media = readyMedia();
            for (const kind of ["image", "video", "audio", "document"]) {
                assert.deepEqual(
                    downloadableMessageMedia({media: [{...media, kind}]}),
                    [
                        {
                            id: 8,
                            label: `Baixar ${
                                {
                                    image: "imagem",
                                    video: "vídeo",
                                    audio: "áudio",
                                    document: "documento",
                                }[kind]
                            }`,
                            name: "voz.ogg",
                            download_url: media.download_url,
                        },
                    ]
                );
            }
            const rejected = [
                null,
                {...media, state: "pending"},
                {...media, state: "failed"},
                {...media, id: "8"},
                {...media, id: 9},
                {...media, kind: "constructor"},
                {...media, content_url: "https://example.com/private.ogg"},
                {...media, content_url: "//example.com/private.ogg"},
                {...media, content_url: "/contact_center/media/8/content?token=secret"},
            ];
            assert.deepEqual(downloadableMessageMedia({media: rejected}), []);
            assert.deepEqual(downloadableMessageMedia({media: [media, media]}), [
                {
                    id: 8,
                    label: "Baixar áudio",
                    name: "voz.ogg",
                    download_url: media.download_url,
                },
            ]);
            for (const download_url of [
                "javascript:alert(1)", // eslint-disable-line no-script-url
                "https://example.com/private.ogg",
                "/contact_center/media/9/content?download=1",
                false,
            ]) {
                assert.strictEqual(
                    downloadableMessageMedia({media: [{...media, download_url}]})[0]
                        .download_url,
                    media.download_url,
                    "unsafe download URL is replaced with the validated local content route"
                );
            }
        }
    );

    QUnit.test(
        "downloads respect tombstones and ignore control event content",
        (assert) => {
            const message = {media: [readyMedia()]};
            assert.deepEqual(downloadableMessageMedia(null), []);
            assert.deepEqual(downloadableMessageMedia({media: {id: 8}}), []);
            assert.deepEqual(
                downloadableMessageMedia({...message, is_deleted: true}),
                []
            );
            assert.deepEqual(
                downloadableMessageMedia({
                    ...message,
                    is_deleted: true,
                    deleted_content_visible: "true",
                }),
                []
            );
            assert.strictEqual(
                downloadableMessageMedia({
                    ...message,
                    is_deleted: true,
                    deleted_content_visible: true,
                }).length,
                1
            );
            assert.deepEqual(
                downloadableMessageMedia({...message, content_type: "call.accept"}),
                []
            );
            assert.strictEqual(
                downloadableMessageMedia({media: [readyMedia({name: " "})]})[0].name,
                "Áudio"
            );
        }
    );

    QUnit.test(
        "read-only messages retain their download menu without mutation actions",
        (assert) => {
            const timeline = {
                store: {capabilities: {}},
                messageActionAllowed: () => false,
            };
            const message = {media: [readyMedia()]};
            assert.ok(
                ConversationTimeline.prototype.hasActions.call(timeline, message)
            );
            assert.notOk(
                ConversationTimeline.prototype.hasActions.call(timeline, {
                    media: [readyMedia({state: "pending"})],
                })
            );
            assert.notOk(
                ConversationTimeline.prototype.hasActions.call(timeline, {
                    ...message,
                    is_deleted: true,
                })
            );
        }
    );

    QUnit.test(
        "media downloads render only in the message menu with keyboard navigation",
        async (assert) => {
            registry.category("services").add("ui", uiService);
            registry.category("services").add("orm", ormService);
            registry.category("services").add("action", {
                start: () => ({doAction: async () => undefined}),
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
            store.state.bootstrap = {capabilities: {}};
            store.state.selectedChannelId = 10;
            store.state.conversations = [{channel_id: 10, state: "resolved"}];
            store.state.timelinePhase = "ready";
            store.state.messages = [
                {
                    message_id: 84,
                    content_type: "media",
                    body_text: "",
                    date: "2026-09-09 12:00:00",
                    direction: "inbound",
                    origin: "provider",
                    platform: "whatsapp",
                    provider: "wuzapi",
                    actions: {reply: false, react: false, edit: false, delete: false},
                    media: [
                        readyMedia(),
                        readyMedia({
                            id: 9,
                            kind: "document",
                            name: "proposta.docx",
                            content_url: "/contact_center/media/9/content",
                            download_url: "/contact_center/media/9/content?download=1",
                        }),
                    ],
                    reactions: [],
                },
            ];
            try {
                await mount(ConversationTimeline, target, {
                    env,
                    props: {state: store.state, store},
                });
                assert.notOk(target.querySelector(".cc-message__bubble a[download]"));
                assert.notOk(target.querySelector(".cc-audio-player__download"));
                assert.strictEqual(
                    target.querySelectorAll(".cc-media-document--file").length,
                    1
                );
                await click(target, ".cc-message-actions__toggle");
                const menu = target.querySelector(".cc-message-menu");
                const links = [...menu.querySelectorAll('[role="menuitem"]')];
                assert.strictEqual(links.length, 2);
                assert.deepEqual(
                    links.map((link) => link.getAttribute("href")),
                    [
                        "/contact_center/media/8/content?download=1",
                        "/contact_center/media/9/content?download=1",
                    ]
                );
                assert.deepEqual(
                    links.map((link) => link.download),
                    ["voz.ogg", "proposta.docx"]
                );
                assert.deepEqual(
                    links.map((link) => link.textContent.trim()),
                    ["Baixar áudio", "Baixar documento"]
                );
                menu.dispatchEvent(
                    new KeyboardEvent("keydown", {key: "End", bubbles: true})
                );
                assert.strictEqual(document.activeElement, links[1]);
                menu.dispatchEvent(
                    new KeyboardEvent("keydown", {key: "Escape", bubbles: true})
                );
                await nextTick();
                assert.notOk(target.querySelector(".cc-message-menu"));
                assert.strictEqual(
                    document.activeElement,
                    target.querySelector(".cc-message-actions__toggle")
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "audio-only messages share the player footer with timestamp and delivery",
        async (assert) => {
            const message = timelineMessage();
            assert.ok(
                hasCompactAudio(message),
                "empty structured envelope is ordinary audio"
            );
            assert.notOk(hasCompactAudio({...message, media: [null]}));
            assert.notOk(hasCompactAudio({...message, body_text: "Legenda"}));
            assert.notOk(hasCompactAudio({...message, is_deleted: true}));
            const fixture = await mountedTimeline([message]);
            try {
                await fixture.settle();
                const audioFooter = fixture.target.querySelector(
                    ".cc-audio-player__footer"
                );
                const times = audioFooter.querySelectorAll("time");
                assert.strictEqual(
                    times.length,
                    2,
                    "duration and sent time share one footer"
                );
                assert.notOk(
                    fixture.target.querySelector(".cc-message__bubble > footer")
                );
                assert.ok(
                    Math.abs(
                        times[0].getBoundingClientRect().top -
                            times[1].getBoundingClientRect().top
                    ) < 5,
                    "audio duration and sent time stay on the same row"
                );
            } finally {
                fixture.close();
            }
        }
    );

    QUnit.test("outbound action menu stays inside the timeline", async (assert) => {
        const fixture = await mountedTimeline([
            timelineMessage({body_text: "Mensagem longa ".repeat(12)}),
        ]);
        try {
            await fixture.settle();
            await click(fixture.target, ".cc-message-actions__toggle");
            await fixture.settle();
            const menu = fixture.target.querySelector(".cc-message-menu");
            const bounds = fixture.timeline.viewportRef.el.getBoundingClientRect();
            const menuBounds = menu.getBoundingClientRect();
            assert.strictEqual(getComputedStyle(menu).position, "fixed");
            assert.ok(
                menuBounds.left >= bounds.left - 1,
                "no clipping behind the inbox list"
            );
            assert.ok(
                menuBounds.right <= bounds.right + 1,
                "no clipping behind the contact panel"
            );
            assert.ok(
                menuBounds.top >= bounds.top - 1 &&
                    menuBounds.bottom <= bounds.bottom + 1,
                "native positioning flips above or below within the visible viewport"
            );
        } finally {
            fixture.close();
        }
    });

    QUnit.test(
        "day headings remain visible while scrolling inside their day",
        async (assert) => {
            const messages = [];
            for (let day = 7; day <= 9; day++) {
                for (let index = 0; index < 6; index++) {
                    messages.push(
                        timelineMessage({
                            message_id: day * 10 + index,
                            date: `2026-09-0${day} 12:0${index}:00`,
                            body_text: "Mensagem do dia ".repeat(8),
                            media: [],
                        })
                    );
                }
            }
            const fixture = await mountedTimeline(messages);
            try {
                await fixture.settle();
                const viewport = fixture.timeline.viewportRef.el;
                viewport.scrollTop = 150;
                await fixture.settle();
                const headings = fixture.target.querySelectorAll(".cc-day-divider");
                const top = viewport.getBoundingClientRect().top;
                assert.strictEqual(headings.length, 3);
                assert.ok(
                    Math.abs(headings[0].getBoundingClientRect().top - top) < 2,
                    "first day stays pinned after its separator scrolls away"
                );
                viewport.scrollTop +=
                    headings[1].getBoundingClientRect().top - top + 100;
                await fixture.settle();
                assert.ok(
                    Math.abs(headings[1].getBoundingClientRect().top - top) < 2,
                    "next day replaces the preceding heading"
                );
                assert.ok(
                    headings[0].getBoundingClientRect().bottom <= top + 2,
                    "preceding day is bounded by its own section"
                );
            } finally {
                fixture.close();
            }
        }
    );

    QUnit.test(
        "video opens in a non-modal floating player and closes cleanly",
        async (assert) => {
            const fixture = await mountedTimeline([
                timelineMessage({media: [readyMedia({kind: "video"})]}),
            ]);
            try {
                await fixture.settle();
                await click(fixture.target, ".cc-media-video");
                const player = document.querySelector(".cc-floating-video");
                assert.ok(player, "player is outside the clipped timeline");
                assert.notOk(fixture.target.contains(player));
                assert.notOk(
                    document.querySelector(".cc-media-viewer-dialog"),
                    "chat remains available"
                );
                assert.ok(player.querySelector("video").controls);
                await click(player, '[aria-label="Fechar vídeo"]');
                assert.notOk(document.querySelector(".cc-floating-video"));
                assert.strictEqual(
                    document.activeElement,
                    fixture.target.querySelector(".cc-media-video")
                );
            } finally {
                fixture.close();
            }
        }
    );

    QUnit.test(
        "live redaction closes the floating video and native PiP while retained media stays open",
        async (assert) => {
            const fixture = await mountedTimeline([
                timelineMessage({media: [readyMedia({kind: "video"})]}),
            ]);
            const pipDescriptor = Object.getOwnPropertyDescriptor(
                document,
                "pictureInPictureElement"
            );
            const exitDescriptor = Object.getOwnPropertyDescriptor(
                document,
                "exitPictureInPicture"
            );
            let pauses = 0;
            let exits = 0;
            try {
                await fixture.settle();
                await click(fixture.target, ".cc-media-video");
                const player = document.querySelector(".cc-floating-video");
                const video = player.querySelector("video");
                video.pause = () => pauses++;
                Object.defineProperty(document, "pictureInPictureElement", {
                    configurable: true,
                    value: video,
                });
                Object.defineProperty(document, "exitPictureInPicture", {
                    configurable: true,
                    value: async () => exits++,
                });
                fixture.store.state.messages[0] = {
                    ...fixture.store.state.messages[0],
                    is_deleted: true,
                    deleted_content_visible: true,
                };
                await fixture.settle();
                assert.strictEqual(
                    document.querySelector(".cc-floating-video"),
                    player,
                    "explicitly retained deleted content keeps its player"
                );
                assert.strictEqual(exits, 0);
                fixture.store.state.messages[0] = {
                    ...fixture.store.state.messages[0],
                    deleted_content_visible: false,
                };
                await fixture.settle();
                assert.notOk(document.querySelector(".cc-floating-video"));
                assert.strictEqual(pauses, 1, "redaction stops the loaded media");
                assert.strictEqual(exits, 1, "redaction exits native PiP as well");
                assert.ok(fixture.target.querySelector(".cc-message-tombstone"));
            } finally {
                fixture.close();
                for (const [key, descriptor] of [
                    ["pictureInPictureElement", pipDescriptor],
                    ["exitPictureInPicture", exitDescriptor],
                ]) {
                    if (descriptor) {
                        Object.defineProperty(document, key, descriptor);
                    } else {
                        delete document[key];
                    }
                }
            }
        }
    );

    QUnit.test(
        "removing the selected media closes its player and does not reopen it on refresh",
        async (assert) => {
            const originalMedia = [readyMedia({kind: "video"})];
            const fixture = await mountedTimeline([
                timelineMessage({media: originalMedia}),
            ]);
            try {
                await fixture.settle();
                await click(fixture.target, ".cc-media-video");
                const video = document.querySelector(".cc-floating-video video");
                let pauses = 0;
                video.pause = () => pauses++;
                fixture.store.state.messages[0] = {
                    ...fixture.store.state.messages[0],
                    media: [],
                };
                await fixture.settle();
                assert.notOk(document.querySelector(".cc-floating-video"));
                assert.strictEqual(pauses, 1);
                fixture.store.state.messages[0] = {
                    ...fixture.store.state.messages[0],
                    media: originalMedia,
                };
                await fixture.settle();
                assert.ok(fixture.target.querySelector(".cc-media-video"));
                assert.notOk(
                    document.querySelector(".cc-floating-video"),
                    "a later DTO refresh does not resume the closed player"
                );
            } finally {
                fixture.close();
            }
        }
    );

    QUnit.test("starting audio pauses the active floating video", async (assert) => {
        const fixture = await mountedTimeline([
            timelineMessage({media: [readyMedia({kind: "video"})]}),
            timelineMessage({message_id: 85}),
        ]);
        try {
            await fixture.settle();
            await click(fixture.target, ".cc-media-video");
            const video = document.querySelector(".cc-floating-video video");
            const audio = fixture.target.querySelector("audio");
            let pauses = 0;
            video.pause = () => pauses++;
            video.dispatchEvent(new Event("play"));
            audio.dispatchEvent(new Event("play"));
            await fixture.settle();
            assert.strictEqual(pauses, 1);
            assert.ok(document.querySelector(".cc-floating-video"));
            assert.strictEqual(
                fixture.target
                    .querySelector(".cc-audio-player__play")
                    .getAttribute("aria-label"),
                "Pausar áudio"
            );
        } finally {
            fixture.close();
        }
    });

    QUnit.test(
        "native picture-in-picture failure preserves the floating video",
        async (assert) => {
            let calls = 0;
            const component = {
                supportsPictureInPicture: true,
                state: {ready: true, error: ""},
                videoRef: {
                    el: {
                        requestPictureInPicture: async () => {
                            calls++;
                            throw new Error("blocked");
                        },
                    },
                },
            };
            assert.notOk(
                await FloatingVideo.prototype.pictureInPicture.call(component)
            );
            assert.strictEqual(calls, 1);
            assert.ok(component.state.error.includes("Continue assistindo aqui"));
            component.videoRef.el.requestPictureInPicture = async () => {
                calls++;
            };
            assert.ok(await FloatingVideo.prototype.pictureInPicture.call(component));
            assert.strictEqual(component.state.error, "");
            component.supportsPictureInPicture = false;
            assert.notOk(
                await FloatingVideo.prototype.pictureInPicture.call(component)
            );
            assert.strictEqual(calls, 2, "unsupported browsers keep the local player");
        }
    );

    QUnit.test(
        "compact audio retains accessible seeking, time, speed and errors",
        async (assert) => {
            const env = await makeTestEnv();
            const target = getFixture();
            const player = await mount(AudioPlayer, target, {
                env,
                props: {media: readyMedia()},
            });
            const audio = target.querySelector("audio");
            Object.defineProperty(audio, "duration", {get: () => 8});
            Object.defineProperty(audio, "currentTime", {value: 0, writable: true});
            assert.strictEqual(
                target.querySelector(".cc-audio-player").getAttribute("aria-label"),
                "Mensagem de voz"
            );
            assert.notOk(target.querySelector(".cc-audio-player__heading"));
            assert.notOk(target.querySelector("a[download]"));
            assert.strictEqual(target.querySelector("time").textContent, "0:08");
            assert.strictEqual(
                target.querySelectorAll(".cc-audio-wave__bar").length,
                24
            );
            const range = target.querySelector('input[type="range"]');
            assert.strictEqual(range.getAttribute("aria-valuetext"), "0:00 de 0:08");
            range.value = "4";
            range.dispatchEvent(new Event("input", {bubbles: true}));
            await nextTick();
            assert.strictEqual(audio.currentTime, 4);
            assert.strictEqual(target.querySelector("time").textContent, "0:04");
            assert.strictEqual(
                target.querySelectorAll(".cc-audio-wave__bar.is-played").length,
                12
            );
            assert.strictEqual(
                target.querySelector(".cc-audio-scrub__thumb").style.left,
                "50%"
            );
            await click(target, ".cc-audio-player__speed");
            assert.strictEqual(audio.playbackRate, 1.5);
            await click(target, ".cc-audio-player__speed");
            assert.strictEqual(audio.playbackRate, 2);
            await click(target, ".cc-audio-player__speed");
            assert.strictEqual(audio.playbackRate, 1);
            player.onSeek({target: {value: "99"}});
            assert.strictEqual(audio.currentTime, 8);
            player.onSeek({target: {value: "-99"}});
            assert.strictEqual(audio.currentTime, 0);
            audio.dispatchEvent(new Event("error"));
            await nextTick();
            assert.ok(target.querySelector(".cc-audio-player__play").disabled);
            assert.ok(range.disabled);
            assert.ok(target.querySelector('[role="alert"]'));
        }
    );

    QUnit.test(
        "audio keeps single playback and recovers display after ending",
        async (assert) => {
            let paused = 0;
            const first = {audio: {pause: () => paused++}, state: {}};
            const second = {audio: {pause: () => undefined}, state: {}};
            AudioPlayer.prototype.onPlay.call(first);
            AudioPlayer.prototype.onPlay.call(second);
            assert.strictEqual(paused, 1);
            assert.ok(second.state.playing);
            AudioPlayer.prototype.onEnded.call(second);
            assert.strictEqual(second.state.currentTime, 0);
            assert.notOk(second.state.playing);
            const rejected = {
                audio: {
                    paused: true,
                    play: async () => {
                        throw new Error("Not supported");
                    },
                },
                state: {error: false, playing: false},
            };
            await AudioPlayer.prototype.togglePlayback.call(rejected);
            assert.ok(rejected.state.error);
            assert.notOk(rejected.state.playing);
        }
    );
});
