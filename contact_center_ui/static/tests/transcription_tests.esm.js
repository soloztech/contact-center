/** @odoo-module **/

/* global QUnit */

import {
    AudioTranscription,
    transcriptionForMedia,
} from "@contact_center_ui/js/transcription.esm";
import {Component, reactive, xml} from "@odoo/owl";
import {click, getFixture, mount, nextTick} from "@web/../tests/helpers/utils";
import {MessageContent} from "@contact_center_ui/js/message_content.esm";
import {dialogService} from "@web/core/dialog/dialog_service";
import {hotkeyService} from "@web/core/hotkeys/hotkey_service";
import {makeFakeLocalizationService} from "@web/../tests/helpers/mock_services";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";
import {ormService} from "@web/core/orm_service";
import {registry} from "@web/core/registry";
import {uiService} from "@web/core/ui/ui_service";

function audioMedia(overrides = {}) {
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

function transcription(overrides = {}) {
    return {
        media_id: 8,
        state: "idle",
        text: "",
        can_request: true,
        error_code: "",
        ...overrides,
    };
}

function messageWithTranscription(descriptor = transcription(), overrides = {}) {
    return {
        message_id: 84,
        channel_id: 10,
        content_type: "media",
        body_text: "",
        direction: "inbound",
        is_deleted: false,
        media: [audioMedia()],
        transcriptions: [descriptor],
        ...overrides,
    };
}

class MessageHarness extends Component {}
MessageHarness.components = {MessageContent};
MessageHarness.props = {message: Object};
MessageHarness.template = xml`
    <MessageContent message="props.message">
        <t t-set-slot="metadata"><span class="test-audio-metadata">10:31</span></t>
    </MessageContent>
`;

QUnit.module("contact_center_ui transcription", (hooks) => {
    hooks.beforeEach(() => {
        registry.category("services").add("orm", ormService);
        registry.category("services").add("ui", uiService);
        registry.category("services").add("hotkey", hotkeyService);
        registry.category("services").add("dialog", dialogService);
        makeFakeLocalizationService();
    });

    QUnit.test("only the matching visible audio can expose a transcript", (assert) => {
        const descriptor = transcription({state: "done", text: "Texto privado"});
        const media = audioMedia();
        const message = messageWithTranscription(descriptor);
        assert.strictEqual(transcriptionForMedia(message, media).text, "Texto privado");
        assert.notOk(transcriptionForMedia({...message, is_deleted: true}, media));
        assert.notOk(transcriptionForMedia(message, audioMedia({id: 9})));
        assert.notOk(transcriptionForMedia(message, audioMedia({id: "8"})));
        assert.notOk(transcriptionForMedia(message, audioMedia({kind: "document"})));
        assert.notOk(transcriptionForMedia(message, audioMedia({state: "failed"})));
        assert.notOk(transcriptionForMedia({...message, transcriptions: null}, media));
        assert.notOk(
            transcriptionForMedia(
                messageWithTranscription(transcription({state: "unknown"})),
                media
            )
        );
        assert.strictEqual(
            transcriptionForMedia(
                messageWithTranscription(transcription({text: "Stale text"})),
                media
            ).text,
            "",
            "only completed results expose text"
        );
        assert.notOk(
            transcriptionForMedia(
                messageWithTranscription(transcription(), {
                    is_deleted: true,
                    deleted_content_visible: true,
                }),
                media
            ).can_request,
            "retained deleted content cannot trigger new processing"
        );
    });

    QUnit.test(
        "unconfigured audio stays unchanged without background RPC",
        async (assert) => {
            let calls = 0;
            const env = await makeTestEnv({
                mockRPC: () => {
                    calls++;
                    return false;
                },
            });
            const target = getFixture();
            const component = await mount(MessageHarness, target, {
                env,
                props: {
                    message: messageWithTranscription(
                        transcription({can_request: false})
                    ),
                },
            });
            assert.containsOnce(target, ".cc-audio-player");
            assert.containsOnce(target, ".test-audio-metadata");
            assert.containsNone(target, ".cc-audio-transcription");
            assert.strictEqual(calls, 0);
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "template extension preserves player slots and escapes transcript text",
        async (assert) => {
            const text = '<img src=x onerror="alert(1)"> Pedido de 220 volts.';
            const env = await makeTestEnv();
            const target = getFixture();
            const component = await mount(MessageHarness, target, {
                env,
                props: {
                    message: messageWithTranscription(
                        transcription({state: "done", text}),
                        {
                            media: [
                                audioMedia(),
                                audioMedia({
                                    id: 9,
                                    kind: "document",
                                    name: "pedido.txt",
                                    content_url: "/contact_center/media/9/content",
                                    download_url:
                                        "/contact_center/media/9/content?download=1",
                                }),
                            ],
                        }
                    ),
                },
            });
            assert.containsOnce(target, ".cc-audio-player");
            assert.containsOnce(
                target,
                ".cc-audio-player__metadata .test-audio-metadata"
            );
            assert.containsOnce(target, ".cc-media-document--file");
            assert.containsOnce(target, ".cc-audio-transcription");
            assert.strictEqual(
                target.querySelector(".cc-audio-transcription__text").textContent,
                text
            );
            assert.containsNone(target, ".cc-audio-transcription img");
            assert.containsNone(target, ".cc-audio-transcription__request");
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "one request queues work and a timeline refresh replaces pending state",
        async (assert) => {
            let calls = 0;
            let resolveRequest = null;
            const response = new Promise((resolve) => {
                resolveRequest = resolve;
            });
            const env = await makeTestEnv({
                mockRPC: (_route, params) => {
                    calls++;
                    assert.strictEqual(params.model, "contact.center.ui.api");
                    assert.strictEqual(params.method, "request_transcription");
                    assert.deepEqual(params.args, [8]);
                    return response;
                },
            });
            const target = getFixture();
            const message = reactive(messageWithTranscription());
            const component = await mount(AudioTranscription, target, {
                env,
                props: {message, media: message.media[0]},
            });
            await click(target, ".cc-audio-transcription__request");
            await component.request();
            assert.strictEqual(calls, 1, "a rapid second request is ignored");
            assert.containsNone(target, ".cc-audio-transcription__request");
            assert.ok(target.textContent.includes("Transcrevendo áudio"));
            resolveRequest(transcription({state: "pending", can_request: false}));
            await nextTick();
            await nextTick();
            assert.ok(target.textContent.includes("Transcrevendo áudio"));
            message.transcriptions = [
                transcription({state: "done", text: "220 volts"}),
            ];
            await nextTick();
            assert.strictEqual(
                target.querySelector(".cc-audio-transcription__text").textContent,
                "220 volts"
            );
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "late RPC response cannot overwrite a completed timeline update",
        async (assert) => {
            let resolveRequest = null;
            const response = new Promise((resolve) => {
                resolveRequest = resolve;
            });
            const env = await makeTestEnv({mockRPC: () => response});
            const message = reactive(messageWithTranscription());
            const target = getFixture();
            const component = await mount(AudioTranscription, target, {
                env,
                props: {message, media: message.media[0]},
            });
            const request = component.request();
            message.transcriptions = [
                transcription({state: "done", text: "Resultado novo"}),
            ];
            await nextTick();
            assert.strictEqual(
                target.querySelector(".cc-audio-transcription__text").textContent,
                "Resultado novo",
                "the bus result is visible even before the RPC resolves"
            );
            resolveRequest(transcription({state: "pending", can_request: false}));
            await request;
            await nextTick();
            assert.strictEqual(
                target.querySelector(".cc-audio-transcription__text").textContent,
                "Resultado novo"
            );
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "failed request permits retry without exposing technical errors",
        async (assert) => {
            let calls = 0;
            const env = await makeTestEnv({
                mockRPC: () => {
                    calls++;
                    if (calls === 1) {
                        throw new Error("provider endpoint token=secret");
                    }
                    return transcription({state: "pending", can_request: false});
                },
            });
            const target = getFixture();
            const message = messageWithTranscription();
            const component = await mount(AudioTranscription, target, {
                env,
                props: {message, media: message.media[0]},
            });
            await component.request();
            await nextTick();
            assert.ok(target.textContent.includes("Não foi possível transcrever"));
            assert.ok(target.textContent.includes("Tentar novamente"));
            assert.notOk(target.textContent.includes("secret"));
            await component.request();
            await nextTick();
            assert.strictEqual(calls, 2);
            assert.ok(target.textContent.includes("Transcrevendo áudio"));
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "a retried job can fail again without restoring stale pending state",
        async (assert) => {
            const env = await makeTestEnv({
                mockRPC: () => transcription({state: "pending", can_request: false}),
            });
            const target = getFixture();
            const message = reactive(
                messageWithTranscription(transcription({state: "failed"}))
            );
            const component = await mount(AudioTranscription, target, {
                env,
                props: {message, media: message.media[0]},
            });
            assert.containsOnce(target, ".cc-audio-transcription__request");
            await component.request();
            await nextTick();
            message.transcriptions = [
                transcription({state: "pending", can_request: false}),
            ];
            await nextTick();
            assert.ok(target.textContent.includes("Transcrevendo áudio"));
            message.transcriptions = [transcription({state: "failed"})];
            await nextTick();
            assert.containsOnce(target, ".cc-audio-transcription__request");
            assert.notOk(target.textContent.includes("Transcrevendo áudio"));
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "long results expand and redaction removes already rendered text",
        async (assert) => {
            const text = "Uma mensagem longa com medidas e quantidades. ".repeat(20);
            const env = await makeTestEnv();
            const target = getFixture();
            const message = reactive(
                messageWithTranscription(transcription({state: "done", text}))
            );
            const component = await mount(AudioTranscription, target, {
                env,
                props: {message, media: message.media[0]},
            });
            assert.ok(
                target.querySelector(".cc-audio-transcription__text").textContent
                    .length < text.length
            );
            await click(target, ".cc-audio-transcription__expand");
            assert.strictEqual(
                target.querySelector(".cc-audio-transcription__text").textContent,
                text
            );
            assert.strictEqual(
                target
                    .querySelector(".cc-audio-transcription__expand")
                    .getAttribute("aria-expanded"),
                "true"
            );
            message.is_deleted = true;
            await nextTick();
            assert.containsNone(target, ".cc-audio-transcription");
            assert.notOk(target.textContent.includes("medidas e quantidades"));
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "copy uses the full text and handles clipboard denial",
        async (assert) => {
            const text = "Medida 220 volts. ".repeat(40);
            const env = await makeTestEnv();
            const target = getFixture();
            const message = messageWithTranscription(
                transcription({state: "done", text})
            );
            const component = await mount(AudioTranscription, target, {
                env,
                props: {message, media: message.media[0]},
            });
            const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
            let copiedText = "";
            let denyCopy = false;
            Object.defineProperty(navigator, "clipboard", {
                configurable: true,
                value: {
                    writeText: async (value) => {
                        if (denyCopy) {
                            throw new Error("Clipboard permission denied");
                        }
                        copiedText = value;
                    },
                },
            });
            try {
                await click(target, ".cc-audio-transcription__copy");
                assert.strictEqual(
                    copiedText,
                    text,
                    "copy includes the collapsed portion"
                );
                assert.ok(target.textContent.includes("Copiado"));
                denyCopy = true;
                await click(target, ".cc-audio-transcription__copy");
                assert.ok(target.textContent.includes("Selecione o texto para copiar"));
                assert.strictEqual(
                    target.querySelector(".cc-audio-transcription__text").textContent,
                    text,
                    "clipboard denial expands the selectable text"
                );
            } finally {
                if (original) {
                    Object.defineProperty(navigator, "clipboard", original);
                } else {
                    delete navigator.clipboard;
                }
                component.__owl__.app.destroy();
            }
        }
    );

    QUnit.test("a failed job only offers retry when authorized", async (assert) => {
        const env = await makeTestEnv();
        const target = getFixture();
        const message = reactive(
            messageWithTranscription(
                transcription({state: "failed", error_code: "internal_secret"})
            )
        );
        const component = await mount(AudioTranscription, target, {
            env,
            props: {message, media: message.media[0]},
        });
        assert.containsOnce(target, ".cc-audio-transcription__request");
        assert.ok(target.textContent.includes("Tentar novamente"));
        assert.notOk(target.textContent.includes("internal_secret"));
        message.transcriptions = [transcription({state: "failed", can_request: false})];
        await nextTick();
        assert.containsNone(target, ".cc-audio-transcription__request");
        assert.ok(target.textContent.includes("Não foi possível transcrever"));
        component.__owl__.app.destroy();
    });
});
