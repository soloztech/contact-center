/** @odoo-module **/

/* global QUnit */

import {
    MessageLinkPreviews,
    safePreviewUrl,
} from "@contact_center_ui/js/link_preview.esm";
import {getFixture, mount} from "@web/../tests/helpers/utils";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";

QUnit.module("contact_center_ui > native link previews", () => {
    QUnit.test(
        "preview links reject active content and malformed native records",
        (assert) => {
            for (const value of [
                null,
                "javascript:alert(1)",
                "//example.com",
                "https://user:secret@example.com",
                "data:text/html,x",
            ]) {
                assert.strictEqual(safePreviewUrl(value), "");
            }
            const component = {
                props: {
                    message: {
                        link_previews: [
                            null,
                            {id: 1, source_url: "javascript:alert(1)"},
                            {id: 2, source_url: "https://example.com"},
                            {id: 2, source_url: "https://example.com/duplicate"},
                        ],
                    },
                },
            };
            const items = Object.getOwnPropertyDescriptor(
                MessageLinkPreviews.prototype,
                "previews"
            ).get.call(component);
            assert.strictEqual(items.length, 1);
            assert.strictEqual(items[0].hostname, "example.com");
        }
    );

    QUnit.test(
        "native metadata is escaped and opens a separate safe tab",
        async (assert) => {
            const env = await makeTestEnv();
            const target = getFixture();
            await mount(MessageLinkPreviews, target, {
                env,
                props: {
                    message: {
                        message_id: 1,
                        link_previews: [
                            {
                                id: 1,
                                source_url: "https://example.com",
                                og_title: "<script>unsafe</script>",
                                og_description: "Details",
                            },
                        ],
                    },
                },
            });
            const link = target.querySelector(".cc-link-preview");
            assert.strictEqual(link.target, "_blank");
            assert.strictEqual(link.rel, "noopener noreferrer");
            assert.ok(link.textContent.includes("<script>unsafe</script>"));
            assert.notOk(link.querySelector("script"));
        }
    );
});
